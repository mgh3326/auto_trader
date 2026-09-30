"""#943 wiring: where the P follow hooks run, and where they must never run."""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]

# The only modules allowed to reach the auto-follow writer.  A read path
# (holdings, /invest settings, get_* MCP tools) or the send-time guard must
# never appear here (#943 ruling: no writes in read or guard paths).
ALLOWED_IMPORTERS = {
    "app/tasks/execution_ledger.py",
    "app/tasks/protected_position_tasks.py",
    "app/mcp_server/tooling/toss_live_ledger.py",
    "scripts/reconcile_execution_ledger.py",
    "scripts/protected_positions.py",
}


class _Session:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        self.events.append("commit")

    async def rollback(self) -> None:
        self.events.append("rollback")


class _Diff:
    def model_dump(self, *, mode: str) -> dict[str, Any]:
        return {"ok": True}


def _patch_task(monkeypatch: pytest.MonkeyPatch, *, commit_enabled: bool) -> list:
    from app.tasks import execution_ledger as mod

    events: list[Any] = []

    class Reconciler:
        def __init__(self, repository: object) -> None:
            self.committed_fill_ids: list[int] = []

        async def run(self, broker: str, *, window_hours: int, dry_run: bool):
            events.append(("run", dry_run))
            if not dry_run:
                self.committed_fill_ids = [11, 12]
            return _Diff()

    async def follow(ids):
        events.append(("follow", list(ids)))
        return []

    monkeypatch.setattr(mod.settings, "EXECUTION_LEDGER_COMMIT_ENABLED", commit_enabled)
    monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: _Session(events))
    monkeypatch.setattr(mod, "ExecutionLedgerReconciler", Reconciler)
    monkeypatch.setattr(mod, "ExecutionLedgerRepository", lambda db: object())
    monkeypatch.setattr(mod, "follow_committed_fills", follow)
    return events


@pytest.mark.unit
def test_reconciler_task_follows_committed_rows_only_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import execution_ledger as mod

    events = _patch_task(monkeypatch, commit_enabled=True)
    asyncio.run(mod.reconcile_execution_ledger_smoke(broker="kis", window_hours=6))
    assert events == [("run", False), "commit", ("follow", [11, 12])]


@pytest.mark.unit
def test_reconciler_task_dry_run_never_follows(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tasks import execution_ledger as mod

    events = _patch_task(monkeypatch, commit_enabled=False)
    asyncio.run(mod.reconcile_execution_ledger_smoke(broker="kis", window_hours=6))
    assert events == [("run", True), "commit"]


@pytest.mark.unit
def test_reconciler_records_only_committed_row_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.execution_ledger import reconciler as mod

    class Repo:
        async def classify_fill(self, fill: Any) -> str:
            return fill.status

        async def upsert_fill(self, fill: Any) -> tuple[str, int]:
            return fill.status, fill.row_id

        def record_run(self, run: object) -> None:
            return None

    class Fill:
        def __init__(self, status: str, row_id: int) -> None:
            self.status, self.row_id = status, row_id

        def model_dump(self, **kwargs: Any) -> dict[str, Any]:
            return {}

    async def fetch(self, *args: Any, **kwargs: Any) -> list[Fill]:
        return [Fill("inserted", 21), Fill("updated", 22), Fill("unchanged", 23)]

    monkeypatch.setattr(mod.settings, "EXECUTION_LEDGER_COMMIT_ENABLED", True)
    monkeypatch.setattr(mod.ExecutionLedgerReconciler, "_fetch_normalized", fetch)
    monkeypatch.setattr(mod, "ExecutionLedgerRead", lambda **kwargs: object())
    monkeypatch.setattr(mod.ReconcileDiff, "add_insert_sample", lambda self, s: None)
    monkeypatch.setattr(mod.ReconcileDiff, "add_update_sample", lambda self, s: None)
    reconciler = mod.ExecutionLedgerReconciler(Repo())  # type: ignore[arg-type]

    asyncio.run(reconciler.run("kis", dry_run=False))
    assert reconciler.committed_fill_ids == [21, 22]

    asyncio.run(reconciler.run("kis", dry_run=True))
    assert reconciler.committed_fill_ids == []


def _function(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        call
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and getattr(call.func, "id", getattr(call.func, "attr", None)) == name
    ]


@pytest.mark.unit
def test_toss_booking_follows_only_after_its_session_block_closes() -> None:
    tree = ast.parse(
        (ROOT / "app/mcp_server/tooling/toss_live_ledger.py").read_text("utf-8")
    )
    function = _function(tree, "_reconcile_one_toss_row")
    booking = next(
        node
        for node in function.body
        if isinstance(node, ast.AsyncWith)
        and _calls(node, "upsert_toss_execution_fill")
    )
    assert not _calls(booking, "follow_committed_fills")
    [hook] = _calls(function, "follow_committed_fills")
    assert hook.lineno > booking.end_lineno  # type: ignore[operator]
    guard = next(
        node
        for node in function.body
        if isinstance(node, ast.If) and _calls(node, "follow_committed_fills")
    )
    assert "execution_status" in ast.unparse(guard.test)
    assert "inserted" in ast.unparse(guard.test)


@pytest.mark.unit
def test_reconcile_script_follows_only_in_commit_mode_after_commit() -> None:
    source = (ROOT / "scripts/reconcile_execution_ledger.py").read_text("utf-8")
    function = _function(ast.parse(source), "_main")
    [hook] = _calls(function, "follow_committed_fills")
    commits = [
        call.lineno
        for call in _calls(function, "commit")
        if "db" in ast.unparse(call.func)
    ]
    assert commits and hook.lineno > max(commits)
    guard = next(
        node
        for node in function.body
        if isinstance(node, ast.If) and _calls(node, "follow_committed_fills")
    )
    assert ast.unparse(guard.test) == "not dry_run"


@pytest.mark.unit
def test_lever_task_is_registered_and_scheduleless() -> None:
    import app.tasks as tasks_pkg
    from app.tasks import protected_position_tasks

    task = protected_position_tasks.protected_positions_auto_follow_reconcile_task
    labels = getattr(task, "labels", {}) or {}
    assert protected_position_tasks in tasks_pkg.TASKIQ_TASK_MODULES
    assert not (labels.get("schedule") if isinstance(labels, dict) else None)
    source = (ROOT / "app/tasks/protected_position_tasks.py").read_text("utf-8")
    assert "schedule=" not in source


@pytest.mark.unit
def test_auto_follow_writer_is_unreachable_from_read_and_guard_paths() -> None:
    pattern = re.compile(r"app\.services\.protected_position_auto_follow\b")
    importers = {
        str(path.relative_to(ROOT))
        for base in ("app", "scripts")
        for path in (ROOT / base).rglob("*.py")
        if path.name != "protected_position_auto_follow.py"
        and pattern.search(path.read_text("utf-8"))
    }
    assert importers == ALLOWED_IMPORTERS
