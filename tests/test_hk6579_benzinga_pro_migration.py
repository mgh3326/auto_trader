"""Render capture migration DDL with the application naming convention."""

from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.models.base import Base


@pytest.mark.unit
@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_source_check_uses_literal_existing_name(monkeypatch, direction):
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "20261125_hk6579_benzinga_pro.py"
    )
    spec = spec_from_file_location("benzinga_pro_revision", path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={
            "as_sql": True,
            "output_buffer": output,
            "target_metadata": Base.metadata,
        },
    )
    operations = Operations(context)
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(
        operations, "get_bind", lambda: SimpleNamespace(scalar=lambda _: False)
    )
    getattr(migration, direction)()
    sql = output.getvalue()
    name = "ck_invest_momentum_event_snapshots_source"
    assert f"DROP CONSTRAINT {name};" in sql
    assert f"ADD CONSTRAINT {name} CHECK" in sql
    assert ("'benzinga_pro'" in sql) == (direction == "upgrade")
