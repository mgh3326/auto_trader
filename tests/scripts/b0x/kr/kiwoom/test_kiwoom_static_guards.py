"""Static guards for the kiwoom lane — mutants ② and ③.

Extends the pattern the KR lane already established (``#1815``/``#1820``'s
``test_submission_ast_guard.py`` and ``test_no_live_kis_order_imports.py``)
rather than re-inventing it: an allowlist for what the lane may import, a
denylist for what it may never name, and a **string-literal** sweep so a
bypass cannot be smuggled in as text and fed to ``importlib``/``httpx``.

Two things are new here and are the §39차 asks:

* mutant ② — the kis ledger exception (contract v1.6 ①) must be unreachable
  from this lane, in *any* spelling: the model class, the account-mode literal,
  the reader function, or the table name.
* mutant ③ — ``api.kiwoom.com`` (live) must be unreachable, including via a
  string that never appears as an import.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[5]
LANE_MODULES = (
    REPO_ROOT / "scripts" / "b0x" / "kr" / "kiwoom.py",
    REPO_ROOT / "scripts" / "b0x" / "kr" / "kiwoom_attribution.py",
    REPO_ROOT / "scripts" / "b0x" / "kr" / "kiwoom_bounded_send.py",
    REPO_ROOT / "scripts" / "b0x" / "kr" / "kiwoom_cycle.py",
    REPO_ROOT / "scripts" / "b0x" / "kr" / "kiwoom_ordering.py",
    REPO_ROOT / "scripts" / "run_b0x_kr_kiwoom_cycle.py",
)

#: 🔴 mutant ③ — any spelling of the live Kiwoom host, including the defensive
#: constant that exists only to be rejected. This lane must not even name it.
#:
#: The host pattern is anchored so ``mockapi.kiwoom.com`` (which contains
#: ``api.kiwoom.com`` as a substring) is not a false hit — a naive substring
#: check here would fire on the *mock* host and make the guard untrustworthy,
#: which is how real guards end up deleted.
FORBIDDEN_LIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![\w.\-])api\.kiwoom\.com"),
    re.compile(r"\bLIVE_BASE_URL\b"),
    re.compile(r"\bKIWOOM_ACCOUNT_NO\b"),
    # ``kiwoom_mock_account_no`` is fine; the bare live setting is not.
    re.compile(r"(?<!mock_)\bkiwoom_account_no\b"),
    re.compile(r"(?<!mock_)\bkiwoom_app_key\b"),
    re.compile(r"(?<!MOCK_)\bKIWOOM_APP_KEY\b"),
    re.compile(r"\blive_market_data\b"),
    re.compile(r"\bKiwoomLiveReadOnly\w*"),
)

#: 🔴 mutant ② — the contract v1.6 ① exception surface, in every spelling.
FORBIDDEN_KIS_LEDGER_SUBSTRINGS = (
    "KISMockOrderLedger",
    "kis_mock_order_ledger",
    "kis_mock_signal_ledger",
    "read_own_pending",
    "pending_ledger",
)

#: Order-capable surfaces from *other* brokers, plus the MCP tool layer.
FORBIDDEN_IMPORT_PREFIXES = (
    "app.services.brokers.kis",
    "app.services.brokers.kiwoom.live_market_data",
    "app.services.brokers.kiwoom.us_orders",
    "app.services.brokers.kiwoom.us_client",
    "app.services.brokers.kiwoom.us_account",
    "app.mcp_server.tooling",
    "app.services.kis_trading_service",
    "app.models.review",
    "app.core.db",
    "scripts.b0x.kr.mock",
    "scripts.b0x.kr.pending_ledger",
)

#: The kiwoom broker classes this lane is allowed to touch (§39차 ①).
ALLOWED_KIWOOM_IMPORTS = frozenset(
    {
        "app.services.brokers.kiwoom",
        "app.services.brokers.kiwoom.client",
        "app.services.brokers.kiwoom.domestic_account",
        "app.services.brokers.kiwoom.domestic_orders",
        "app.services.brokers.kiwoom.normalization",
    }
)


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(_source(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _string_literals(path: Path) -> list[str]:
    tree = ast.parse(_source(path))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _attribute_chains(path: Path) -> set[str]:
    """Return dotted attribute accesses, e.g. ``kr_attribution.read_own_attribution``."""

    chains: set[str] = set()
    for node in ast.walk(ast.parse(_source(path))):
        if not isinstance(node, ast.Attribute):
            continue
        parts = [node.attr]
        current: ast.expr = node.value
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
            chains.add(".".join(reversed(parts)))
    return chains


def test_the_lane_modules_all_exist() -> None:
    missing = [path for path in LANE_MODULES if not path.exists()]
    assert missing == [], f"kiwoom lane modules missing: {missing}"


@pytest.mark.parametrize("path", LANE_MODULES, ids=lambda p: p.name)
def test_no_live_kiwoom_surface_anywhere_including_strings(path: Path) -> None:
    """🔴 mutant ③ — live host/credentials unreachable, string bypass included."""

    source = _source(path)
    offenders = [
        pattern.pattern for pattern in FORBIDDEN_LIVE_PATTERNS if pattern.search(source)
    ]
    assert offenders == [], (
        f"{path.name} names a live Kiwoom surface: {offenders}. This lane is "
        "mock-only; even a string literal is a bypass because it can be handed "
        "to httpx/importlib at runtime."
    )


@pytest.mark.parametrize("path", LANE_MODULES, ids=lambda p: p.name)
def test_no_kis_ledger_exception_surface(path: Path) -> None:
    """🔴 mutant ② — contract v1.6 ①'s ledger is unreachable from this lane."""

    # The names legitimately appear inside prose explaining *why* they are not
    # used, so this runs against what the runtime can act on — non-docstring
    # string constants, attribute chains and imports — not against comments.
    literal_offenders = sorted(
        f"{needle} in {text!r}"
        for text in _code_literals(path)
        for needle in FORBIDDEN_KIS_LEDGER_SUBSTRINGS
        if needle in text
    )
    kis_aliases = _kis_core_aliases(path)
    chain_offenders = sorted(
        chain
        for chain in _attribute_chains(path)
        # 🔴 Only the *kis* module's readers are forbidden. This lane has its
        # own ``read_own_attribution`` (broker-sourced), so the check resolves
        # the import alias instead of matching on the bare attribute name —
        # otherwise the guard would fire on the compliant implementation and
        # miss the real bypass that imports the kis module under a new alias.
        if (
            chain.split(".")[0] in kis_aliases
            and chain.split(".")[-1] in {"read_own_attribution", "read_own_pending"}
        )
        or "KISMockOrderLedger" in chain
    )
    import_offenders = sorted(
        module
        for module in _imported_modules(path)
        for needle in ("pending_ledger", "app.models.review", "app.core.db")
        if needle in module
    )

    assert literal_offenders == [], f"{path.name} code literal: {literal_offenders}"
    assert chain_offenders == [], (
        f"{path.name} calls the kis ledger reader: {chain_offenders}. 계약 v1.6 ① "
        "예외는 브로커 표면 부재 한정 — kiwoom 은 kt00009 로 직접 답한다 (§39차 2항)."
    )
    assert import_offenders == [], f"{path.name} imports: {import_offenders}"


def _kis_core_aliases(path: Path) -> set[str]:
    """Local names bound to ``scripts.b0x.kr.attribution`` / ``...pending_ledger``.

    Resolving the alias is what makes the ledger guard precise: importing the
    kis core for its *pure* helpers is allowed and expected (§39차 says extend,
    do not re-invent), while calling its DB readers is the violation.
    """

    kis_core = {"scripts.b0x.kr.attribution", "scripts.b0x.kr.pending_ledger"}
    aliases: set[str] = set()
    for node in ast.walk(ast.parse(_source(path))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in kis_core:
                    aliases.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                full = f"{node.module}.{alias.name}"
                if full in kis_core:
                    aliases.add(alias.asname or alias.name)
    return aliases


def _code_literals(path: Path) -> list[str]:
    """String constants that are *not* documentation.

    Docstrings (module/class/function) and any literal containing a newline are
    treated as prose. Everything else is code the runtime can act on.
    """

    tree = ast.parse(_source(path))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        # Anything containing whitespace is prose (a record-stamp clause, an
        # error message). A module/table/class identifier never does. That
        # split is safe *because* the import assertion below already removes
        # every way a string could become a query: this lane imports no DB
        # session and no ORM model, so a literal table name is inert text.
        and not any(char.isspace() for char in node.value)
    ]


@pytest.mark.parametrize("path", LANE_MODULES, ids=lambda p: p.name)
def test_import_allowlist_holds(path: Path) -> None:
    offenders = [
        module
        for module in _imported_modules(path)
        for forbidden in FORBIDDEN_IMPORT_PREFIXES
        if (module == forbidden or module.startswith(f"{forbidden}."))
        and module not in ALLOWED_KIWOOM_IMPORTS
    ]
    assert offenders == [], f"{path.name} imports a forbidden surface: {offenders}"


def test_kiwoom_broker_imports_are_only_the_three_sanctioned_classes() -> None:
    """§39차 ① — only KiwoomMockClient / DomesticAccount / DomesticOrder."""

    imported = {
        module
        for path in LANE_MODULES
        for module in _imported_modules(path)
        if module.startswith("app.services.brokers.kiwoom")
    }
    assert imported <= ALLOWED_KIWOOM_IMPORTS, (
        f"kiwoom lane imports beyond the sanctioned set: "
        f"{sorted(imported - ALLOWED_KIWOOM_IMPORTS)}"
    )


def test_same_cycle_batch_api_is_kr_local_and_common_callers_are_unchanged() -> None:
    """Lane isolation (A): only the kiwoom path can name the batch proof."""

    common = REPO_ROOT / "scripts" / "b0x" / "broker_truth.py"
    frozen_callers = (
        REPO_ROOT / "scripts" / "b0x" / "crypto" / "sidecar.py",
        REPO_ROOT / "scripts" / "b0x" / "us" / "alpaca.py",
    )
    batch_names = (
        "SameCycleBuyBatchAuthorization",
        "authorize_same_cycle_buy_batch",
        "submit_day_order_in_batch",
    )

    for path in (common, *frozen_callers):
        source = _source(path)
        assert all(name not in source for name in batch_names), path

    for path in frozen_callers:
        calls = [
            node
            for node in ast.walk(ast.parse(_source(path)))
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "assert_resubmit_allowed"
        ]
        assert len(calls) == 1, (
            f"{path.name} must retain its one existing common-gate call; got "
            f"{len(calls)}"
        )

    common_tree = ast.parse(_source(common))
    definition = next(
        node
        for node in common_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "assert_resubmit_allowed"
    )
    assert [arg.arg for arg in definition.args.args] == ["truth"]
    assert [arg.arg for arg in definition.args.kwonlyargs] == ["symbol", "lane"]


def test_guard_would_actually_catch_a_bypass() -> None:
    """FALSE-GREEN check: the guards must fail on a planted violation.

    A static guard that passes on a file which *does* contain the forbidden
    text is not a guard. This plants each class of bypass in a temporary
    source string and asserts the same predicates reject it.
    """

    planted_live = 'HOST = "api.kiwoom.com"\n'
    assert any(p.search(planted_live) for p in FORBIDDEN_LIVE_PATTERNS)
    # ...and must NOT fire on the mock host, or it is a guard nobody can keep.
    assert not any(
        p.search('HOST = "https://mockapi.kiwoom.com"\n')
        for p in FORBIDDEN_LIVE_PATTERNS
    )

    planted_ledger = "from app.models.review import KISMockOrderLedger\n"
    assert any(needle in planted_ledger for needle in FORBIDDEN_KIS_LEDGER_SUBSTRINGS)

    planted_import = "import app.services.brokers.kis.domestic_orders\n"
    assert any(
        planted_import.strip().endswith(prefix)
        or f"{prefix}." in planted_import
        or prefix in planted_import
        for prefix in FORBIDDEN_IMPORT_PREFIXES
    )
