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

_PRODUCTION_HOST_RE = re.compile(
    r"(?<![\w.\-])api\.nhplug\.com(?![\w.\-])", re.IGNORECASE
)
_VENDOR_IMPORT = "nhplug"
_FORBIDDEN_OVERRIDE_ENV = frozenset({"NHPLUG_BASE_URL", "NHPLUG_AUTH_URL"})
_FORBIDDEN_ORDER_TEXT = (
    "/krstock/order/",
    "/usstock/order/",
    "/krstock/trading/",
    "/usstock/trading/",
    "cashBuy",
    "cashSell",
    "SCSOS61803A",
    "SCSOS61801A",
    "SCSOS61808A",
    "SCSOS61809A",
)
_FORBIDDEN_MUTATION_FRAGMENTS = (
    "order",
    "buy",
    "sell",
    "modify",
    "cancel",
    "submit",
    "place",
    "execute",
    "trade",
)
_HTTPX_CLIENT_NAMES = frozenset({"AsyncClient", "Client"})


def _runtime_package_sources(package_dir: Path = RUNTIME_DIR) -> tuple[Path, ...]:
    """Enumerate every Python source in the package, including future modules."""

    return tuple(
        sorted(
            path
            for path in package_dir.rglob("*.py")
            if "__pycache__" not in path.parts
        )
    )


def _runtime_sources() -> tuple[Path, ...]:
    return _runtime_package_sources() + (SMOKE_SCRIPT,)


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


def _imports_nhplug_auth(tree: ast.AST) -> list[str]:
    """Find direct, absolute, and relative imports of the OAuth owner."""

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders.extend(
                alias.name
                for alias in node.names
                if alias.name == "app.services.brokers.nhplug.auth"
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "app.services.brokers.nhplug.auth" or (
                node.level > 0 and module == "auth"
            ):
                offenders.append(module or ".auth")
            if any(alias.name == "auth" for alias in node.names) and (
                module == "app.services.brokers.nhplug" or node.level > 0
            ):
                offenders.append(f"{module or '.'}.auth")
    return offenders


def _httpx_client_constructions(tree: ast.AST) -> list[ast.Call]:
    """Find qualified and directly imported sync/async httpx client construction."""

    direct_import_names = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "httpx"
        for alias in node.names
        if alias.name in _HTTPX_CLIENT_NAMES
    }
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in _HTTPX_CLIENT_NAMES
            or isinstance(node.func, ast.Name)
            and node.func.id in direct_import_names
        )
    ]


def _pins_follow_redirects_false(call: ast.Call) -> bool:
    return any(
        keyword.arg == "follow_redirects"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is False
        for keyword in call.keywords
    )


def _assert_package_has_no_oauth_imports(package_sources: tuple[Path, ...]) -> None:
    for path in package_sources:
        if path == RUNTIME_DIR / "auth.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders = _imports_nhplug_auth(tree)
        assert not offenders, (
            f"{path.relative_to(RUNTIME_DIR) if path.is_relative_to(RUNTIME_DIR) else path.name} "
            f"imports the OAuth owner: {offenders!r}"
        )


def _assert_package_pins_follow_redirects(package_sources: tuple[Path, ...]) -> None:
    for path in package_sources:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in _httpx_client_constructions(tree):
            assert _pins_follow_redirects_false(call), (
                f"{path.name}:{call.lineno} does not pin follow_redirects=False"
            )
        for call in (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and any(keyword.arg == "follow_redirects" for keyword in node.keywords)
        ):
            assert _pins_follow_redirects_false(call), (
                f"{path.name}:{call.lineno} weakens follow_redirects=False"
            )


def _assert_package_exposes_no_mutation_methods(
    package_sources: tuple[Path, ...],
) -> None:
    offenders = {
        f"{path.name}:{node.name}"
        for path in package_sources
        for node in ast.walk(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        and any(
            fragment in node.name.lower() for fragment in _FORBIDDEN_MUTATION_FRAGMENTS
        )
    }
    assert not offenders, (
        f"stage-one package exposes mutation-like methods: {sorted(offenders)!r}"
    )


def _assert_entire_package_is_stage_one_safe(package_dir: Path) -> None:
    """Apply every package-level guard to every present and future module."""

    package_sources = _runtime_package_sources(package_dir)
    assert package_sources, f"no Python sources found under {package_dir}"
    for path in package_sources:
        _assert_stage_one_source_safe(
            path.read_text(encoding="utf-8"),
            filename=path.name,
            permits_production_host=(
                package_dir == RUNTIME_DIR and path == RUNTIME_DIR / "auth.py"
            ),
        )
    _assert_package_has_no_oauth_imports(package_sources)
    _assert_package_pins_follow_redirects(package_sources)
    _assert_package_exposes_no_mutation_methods(package_sources)


def _assert_stage_one_source_safe(
    source: str, *, filename: str, permits_production_host: bool = False
) -> None:
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
    if not permits_production_host:
        assert not any(_PRODUCTION_HOST_RE.search(literal) for literal in literals), (
            "only auth.py may contain the production hostname"
        )
    assert not any(
        forbidden.casefold() in literal.casefold()
        for literal in literals
        for forbidden in _FORBIDDEN_ORDER_TEXT
    ), "stage-one source contains an out-of-scope order endpoint or TR"


def test_entire_nhplug_package_obeys_every_stage_one_static_guard() -> None:
    """New package files receive the same controls as auth.py and client.py."""

    _assert_entire_package_is_stage_one_safe(RUNTIME_DIR)
    _assert_stage_one_source_safe(
        SMOKE_SCRIPT.read_text(encoding="utf-8"), filename=SMOKE_SCRIPT.name
    )


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
        for path in _runtime_sources()
        if any(
            _PRODUCTION_HOST_RE.search(literal)
            for literal in _literal_strings(ast.parse(path.read_text(encoding="utf-8")))
        )
    ]
    assert owners == ["auth.py"]


@pytest.mark.parametrize(
    ("label", "source", "match"),
    (
        (
            "OAuth import",
            "from app.services.brokers.nhplug import auth\n",
            "OAuth owner",
        ),
        (
            "unpinned HTTPX client",
            "from httpx import AsyncClient\nclient = AsyncClient()\n",
            "follow_redirects=False",
        ),
        (
            "mutation method",
            "async def place_order():\n    return None\n",
            "mutation-like methods",
        ),
    ),
)
def test_new_package_file_mutant_fails_the_full_package_guard(
    tmp_path: Path, label: str, source: str, match: str
) -> None:
    """A newly added module cannot bypass package-wide Stage 1 controls."""

    (tmp_path / "orders.py").write_text(source, encoding="utf-8")

    with pytest.raises(AssertionError, match=match):
        _assert_entire_package_is_stage_one_safe(tmp_path)


def test_nested_auth_named_file_cannot_claim_the_production_host_exception(
    tmp_path: Path,
) -> None:
    nested_auth = tmp_path / "nested" / "auth.py"
    nested_auth.parent.mkdir()
    nested_auth.write_text('HOST = "https://api.nhplug.com:8443"\n', encoding="utf-8")

    with pytest.raises(AssertionError, match="only auth.py"):
        _assert_entire_package_is_stage_one_safe(tmp_path)


def test_dry_run_confirm_contract_is_typed_but_has_no_dispatch_consumer() -> None:
    default = DryRunConfirmContract()
    assert default.dry_run is True
    assert default.confirm is False
    default.assert_dispatch_allowed()
    with pytest.raises(ValueError):
        DryRunConfirmContract(dry_run=False, confirm=False).assert_dispatch_allowed()
    DryRunConfirmContract(dry_run=False, confirm=True).assert_dispatch_allowed()
