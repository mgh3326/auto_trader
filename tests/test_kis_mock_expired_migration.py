"""Task 881 migration keeps the existing lifecycle CHECK in sync."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit


def test_kis_mock_expired_migration_up_and_down(monkeypatch):
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/20260928_task881_kis_mock_expired.py"
    )
    spec = importlib.util.spec_from_file_location("task881_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    observed: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        module.op,
        "drop_constraint",
        lambda *args, **kwargs: observed.append(("drop", args, kwargs)),
    )
    monkeypatch.setattr(
        module.op,
        "create_check_constraint",
        lambda *args, **kwargs: observed.append(("create", args, kwargs)),
    )
    assert module.down_revision == "20260926_task711_dispatch"
    module.upgrade()
    assert observed[0][1][0] == "kis_mock_ledger_lifecycle_state_allowed"
    assert observed[1][1][0] == "kis_mock_ledger_lifecycle_state_allowed"
    assert "'expired'" in observed[1][1][2]
    observed.clear()
    module.downgrade()
    assert "'expired'" not in observed[1][1][2]
