from __future__ import annotations

import importlib.util
import io
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "20261127_hk6302_news_timing.py"
)
SPEC = importlib.util.spec_from_file_location(
    "hk6302_news_timing_migration", MIGRATION_PATH
)
assert SPEC is not None and SPEC.loader is not None
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


def test_research_report_receipt_default_is_expand_only_sql_expression() -> None:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    original_op = MIGRATION.op
    MIGRATION.op = Operations(context)
    try:
        MIGRATION.upgrade()
    finally:
        MIGRATION.op = original_op

    sql = output.getvalue()
    assert "ALTER COLUMN first_received_at SET DEFAULT now()" in sql
    assert "ALTER COLUMN first_received_at SET DEFAULT 'now()'" not in sql
    assert "ALTER COLUMN first_received_at SET NOT NULL" not in sql
    assert "UPDATE research_reports SET first_received_at" not in sql
    assert "SET relation_type = 'direct'" not in sql
