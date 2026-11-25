"""hk 6036 static guard: no broker order or ledger module is reachable.

1. AST import closure of the recorder package and its CLI (function-level imports
   included for those files, module-level imports and parent packages for what
   they reach).
2. A fresh interpreter that imports them all loads none of the denied modules.
3. The two shadow tables have one writer, and it only inserts.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
PACKAGE = REPO / "app" / "services" / "upbit_intraday_shadow"
OWN_MODULES = tuple(
    f"app.services.upbit_intraday_shadow.{p.stem}"
    for p in sorted(PACKAGE.glob("*.py"))
    if p.stem != "__init__"
) + ("scripts.upbit_intraday_shadow",)
FIRST_PARTY = ("app", "research", "scripts")

DENIED_PREFIXES = (
    "app.services.brokers",
    "app.services.execution_intent_gateway",
    "app.services.strategy_sleeves",
    "app.services.mock_strategy",
    "app.services.mechanical_loss_cut",
    "app.services.decision_evidence",
    "app.mcp_server",
    "app.routers",
    "app.tasks",
    "app.models",
)
#: Any module named after orders, ledgers or brokers, wherever it lives. Matched
#: on whole name words, so ``recorder`` does not read as ``order``.
DENIED_WORDS = frozenset({"order", "orders", "ledger", "ledgers", "broker", "brokers"})


def _denied(module: str) -> bool:
    if module.split(".")[0] not in FIRST_PARTY:
        return False
    words = set(re.split(r"[._]", module))
    return module.startswith(DENIED_PREFIXES) or bool(words & DENIED_WORDS)


def _module_file(name: str) -> Path | None:
    base = REPO.joinpath(*name.split("."))
    for candidate in (base / "__init__.py", base.with_suffix(".py")):
        if candidate.is_file():
            return candidate
    return None


def _imports(module: str, path: Path, *, deep: bool) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree) if deep else tree.body:
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                drop = node.level - (1 if path.name == "__init__.py" else 0)
                parent = module.rsplit(".", drop)[0] if drop else module
                base = f"{parent}.{base}" if base else parent
            names = [base] + [
                f"{base}.{a.name}"
                for a in node.names
                if _module_file(f"{base}.{a.name}") is not None
            ]
        found.update(n for n in names if n.split(".")[0] in FIRST_PARTY)
    return found


def _closure(starts: tuple[str, ...]) -> set[str]:
    seen: set[str] = set()
    stack = list(starts)
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        parts = module.split(".")
        stack.extend(".".join(parts[:end]) for end in range(1, len(parts)))
        path = _module_file(module)
        if path is not None:
            stack.extend(_imports(module, path, deep=module in starts))
    return seen


def test_the_import_closure_holds_no_order_ledger_or_broker_module() -> None:
    closure = _closure(OWN_MODULES)
    assert "research.upbit_intraday.rules" in closure
    assert "app.core.db" in closure  # the commit path is covered
    assert sorted(m for m in closure if _denied(m)) == []


@pytest.mark.parametrize(
    "module",
    [
        "app.services.brokers.upbit.client",  # its package imports order submission
        "app.services.execution_ledger.fill_ingest",
        "app.services.toss_live_order_ledger_service",
    ],
)
def test_the_guard_sees_a_forbidden_import(module) -> None:
    assert any(_denied(m) for m in _closure((module,)))


def test_the_word_match_does_not_catch_the_recorder() -> None:
    assert not _denied("app.services.upbit_intraday_shadow.recorder")
    assert _denied("app.services.order_send_intent_service")


def test_importing_the_recorder_loads_no_denied_module(tmp_path) -> None:
    code = (
        "import importlib, json, sys\n"
        f"for name in {list(OWN_MODULES) + ['app.core.db']!r}:\n"
        "    importlib.import_module(name)\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    # Inert placeholders for the required settings; cwd has no .env file.
    env = {
        "PYTHONPATH": str(REPO),
        "HOME": str(tmp_path),
        "KIS_APP_KEY": "placeholder",
        "KIS_APP_SECRET": "placeholder",
        "OPENDART_API_KEY": "placeholder",
        "DATABASE_URL": "postgresql+asyncpg://p:p@127.0.0.1:1/p",
        "UPBIT_ACCESS_KEY": "placeholder",
        "UPBIT_SECRET_KEY": "placeholder",
        "SECRET_KEY": "Inert_Placeholder_Key_0123456789_Not_Real",
    }
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    loaded = json.loads(result.stdout.strip().splitlines()[-1])
    assert "research.upbit_intraday.rules" in loaded
    assert [m for m in loaded if _denied(m)] == []


def test_the_shadow_tables_have_one_insert_only_writer() -> None:
    mentions = {
        path.relative_to(REPO).as_posix()
        for root in ("app", "scripts")
        for path in (REPO / root).rglob("*.py")
        if re.search(r"upbit_shadow_(candidates|virtual_trades)", path.read_text())
    }
    assert mentions == {"app/services/upbit_intraday_shadow/writer.py"}
    source = (PACKAGE / "writer.py").read_text()
    assert set(re.findall(r"research\.upbit_shadow_\w+", source)) == {
        "research.upbit_shadow_candidates",
        "research.upbit_shadow_virtual_trades",
    }
    assert re.search(r"\b(UPDATE|DELETE|TRUNCATE)\b", source) is None


def test_only_the_writer_holds_sql() -> None:
    for path in PACKAGE.glob("*.py"):
        if path.name != "writer.py":
            assert "sqlalchemy import text" not in path.read_text(), path.name
