from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    path = ROOT / "alembic" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_check_expands_then_validates_for_kis_rankings() -> None:
    expand = _load("20261126_hk6758_kr_mover_source")
    validate = _load("20261126_hk6758_kr_mover_source_val")
    assert expand.down_revision == "20261126_hk6750"
    assert len(expand.revision) <= 32
    assert validate.down_revision == expand.revision
    assert len(validate.revision) <= 32
    assert "kis_domestic_ranking" in expand.NEW_SOURCES
    assert "NOT VALID" in inspect.getsource(expand.upgrade)
    assert "VALIDATE CONSTRAINT" in inspect.getsource(validate.upgrade)


def test_downgrade_preserves_rows_and_kis_surface_statuses() -> None:
    expand = _load("20261126_hk6758_kr_mover_source")
    source = inspect.getsource(expand.downgrade)
    assert "LOCK TABLE invest_momentum_event_runs" in source
    assert "source = 'kis_domestic_ranking'" in source
    assert "stock:kis_domestic_ranking:%" in source
    assert "raise RuntimeError" in source
