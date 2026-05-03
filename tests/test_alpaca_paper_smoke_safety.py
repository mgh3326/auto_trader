"""Safety tests for scripts/smoke/alpaca_paper_readonly_smoke.py (ROB-71)."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from app.mcp_server.tooling.alpaca_paper import (
    reset_alpaca_paper_service_factory,
    set_alpaca_paper_service_factory,
)

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "smoke"
    / "alpaca_paper_readonly_smoke.py"
)

FORBIDDEN_VERBS = {
    "submit_order",
    "cancel_order",
    "place_order",
    "modify_order",
    "replace_order",
}


@pytest.mark.unit
def test_smoke_script_exists() -> None:
    assert SCRIPT_PATH.exists(), f"smoke script not found: {SCRIPT_PATH}"


@pytest.mark.unit
def test_smoke_script_no_forbidden_order_verbs() -> None:
    """The smoke script must not reference any mutating order verb."""
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    found = [v for v in FORBIDDEN_VERBS if v in text]
    assert not found, f"smoke script references forbidden order verbs: {found}"


@pytest.mark.unit
def test_smoke_script_references_readonly_tool_names_constant() -> None:
    """The script must use ALPACA_PAPER_READONLY_TOOL_NAMES for inventory coverage."""
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "ALPACA_PAPER_READONLY_TOOL_NAMES" in text


@pytest.mark.unit
def test_smoke_script_no_raw_payload_print() -> None:
    """print() calls must not dump raw broker objects by name."""
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    raw_names = {
        "payload",
        "result",
        "orders",
        "positions",
        "account",
        "fills",
        "assets",
        "order",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "print":
                for arg in node.args:
                    if isinstance(arg, ast.Name) and arg.id in raw_names:
                        pytest.fail(
                            f"smoke script calls print({arg.id}) which would dump a raw broker payload"
                        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_smoke_exits_zero_when_all_tools_succeed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """run_smoke() returns 0 when every tool call succeeds via the fake service."""
    from tests.test_mcp_alpaca_paper_tools import FakeAlpacaPaperService

    async def fake_ledger_list_recent(
        limit: int = 50, lifecycle_state: str | None = None
    ) -> dict:
        assert limit == 1
        assert lifecycle_state is None
        return {"success": True, "count": 0, "items": []}

    async def fake_ledger_get(client_order_id: str) -> dict:
        return {"success": False, "found": False, "client_order_id": client_order_id}

    service = FakeAlpacaPaperService()
    set_alpaca_paper_service_factory(lambda: service)  # type: ignore[arg-type]
    try:
        spec = importlib.util.spec_from_file_location("_alpaca_smoke", SCRIPT_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        module.alpaca_paper_ledger_list_recent = fake_ledger_list_recent
        module.alpaca_paper_ledger_get = fake_ledger_get
        exit_code = await module.run_smoke()
    finally:
        reset_alpaca_paper_service_factory()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "summary: PASS tools_ok=9/9" in captured.out
