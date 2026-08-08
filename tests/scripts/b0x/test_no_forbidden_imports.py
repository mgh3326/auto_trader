"""Static import guard for ``scripts/b0x/**``.

Contract §7: *live 계좌 접촉 0 · 실주문 0 (mock/demo/shadow 만) … in-process LLM 0
· 스케줄러 등록 없음(v1).*

``tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py``
scans ``app/**``; this package lives under ``scripts/``, so it needs its own
guard. This one is also broader: besides LLM providers it forbids live-broker
order surfaces and scheduler registration, which are the two ways an
observation adapter turns into something else.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PACKAGE_ROOT = Path(__file__).resolve().parents[3] / "scripts" / "b0x"
#: Every B0-X entry point outside the package directory. A runner that is not
#: listed here is a runner the guard does not scan, so new CLIs go in this
#: tuple in the same commit that adds them.
RUNNERS = tuple(
    Path(__file__).resolve().parents[3] / "scripts" / name
    for name in ("run_b0x_cycle.py", "run_b0x_kr_cycle.py", "run_b0x_cancel.py")
)

#: In-process LLM providers — the ROB-501 runtime ownership boundary.
FORBIDDEN_LLM = (
    "google.generativeai",
    "google_genai",
    "openai",
    "anthropic",
    "langchain",
    "litellm",
    "vertexai",
    "cohere",
    "mistralai",
    "ollama",
    "transformers",
    "app.services.gemini",
    "app.services.openai",
    "app.services.grok",
)

#: Live/real-money order surfaces. Demo and shadow paths are the whole point;
#: these are the ones that move real assets.
FORBIDDEN_LIVE_ORDER = (
    "app.services.brokers.upbit.orders",
    "app.services.brokers.kis.domestic_orders",
    "app.services.brokers.kis.overseas_orders",
    "app.services.brokers.toss.client",
    "app.services.brokers.kiwoom.domestic_orders",
    "app.services.brokers.alpaca",
    "app.services.kis_trading_service",
    "app.services.order_execution",
    "app.services.live_order_ledger",
    "app.mcp_server.tooling.orders_toss_variants",
    "app.mcp_server.tooling.orders_kiwoom_variants",
    "app.services.brokers.binance.futures_demo",
    "app.services.brokers.binance.rest_client",
)

#: Scheduler registration — v1 is manual kickoff only.
FORBIDDEN_SCHEDULER = (
    "taskiq",
    "prefect",
    "celery",
    "apscheduler",
    "app.flows",
    "app.jobs",
    "app.tasks",
)


def _python_files() -> list[Path]:
    return sorted([*PACKAGE_ROOT.rglob("*.py"), *RUNNERS])


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_the_package_actually_has_files() -> None:
    """A guard over an empty set passes vacuously; make that impossible."""

    files = _python_files()
    assert len(files) >= 8, f"expected the b0x package, found {files}"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_in_process_llm_provider_imports(path: Path) -> None:
    offenders = [
        module
        for module in _imported_modules(path)
        for forbidden in FORBIDDEN_LLM
        if module == forbidden or module.startswith(f"{forbidden}.")
    ]
    assert offenders == [], f"{path.name} imports LLM providers: {offenders}"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_live_order_surface_imports(path: Path) -> None:
    offenders = [
        module
        for module in _imported_modules(path)
        for forbidden in FORBIDDEN_LIVE_ORDER
        if module == forbidden or module.startswith(f"{forbidden}.")
    ]
    assert offenders == [], f"{path.name} imports a live order surface: {offenders}"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_scheduler_registration(path: Path) -> None:
    offenders = [
        module
        for module in _imported_modules(path)
        for forbidden in FORBIDDEN_SCHEDULER
        if module == forbidden or module.startswith(f"{forbidden}.")
    ]
    assert offenders == [], f"{path.name} imports a scheduler: {offenders}"


def test_the_only_binance_surface_is_spot_demo() -> None:
    """Futures Demo stays ``unassigned`` in the account map — B0-X must not
    reach it, and Spot Demo is the only Binance import allowed."""

    binance_imports: set[str] = set()
    for path in _python_files():
        binance_imports |= {
            module for module in _imported_modules(path) if "binance" in module
        }
    assert binance_imports, "expected the sidecar to import Spot Demo"
    for module in binance_imports:
        assert "spot_demo" in module or module.endswith("binance.errors"), module


def test_the_only_upbit_surface_is_the_read_client() -> None:
    upbit_imports: set[str] = set()
    for path in _python_files():
        upbit_imports |= {
            module for module in _imported_modules(path) if "upbit" in module.lower()
        }
    for module in upbit_imports:
        assert module in {
            "app.services.brokers.upbit.client",
            "scripts.policy_table.core.upbit_tick",
        }, module


def test_the_table_generator_is_never_invoked() -> None:
    """§7: 표 생성기와 어댑터는 별개 프로세스 (표는 read-only 유지)."""

    for path in _python_files():
        modules = _imported_modules(path)
        assert "scripts.build_policy_table" not in modules, path.name
        # The read-only core helpers are fine; the adapters are the generator's
        # own fetch paths and must not be driven from here.
        assert not any(
            module.startswith("scripts.policy_table.adapters") for module in modules
        ), path.name


def test_no_module_writes_into_the_table_directory() -> None:
    """The policy-tables directory is the generator's; B0-X only reads it."""

    from scripts.b0x.ledger import DEFAULT_OBSERVATION_DIR
    from scripts.b0x.table_source import DEFAULT_TABLE_DIR

    assert DEFAULT_TABLE_DIR not in DEFAULT_OBSERVATION_DIR.parents
    assert DEFAULT_OBSERVATION_DIR != DEFAULT_TABLE_DIR
    # And the observation root is outside the PR-only operator repo entirely.
    assert "auto_trader-operator" not in str(DEFAULT_OBSERVATION_DIR)
