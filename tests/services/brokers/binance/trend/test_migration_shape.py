"""hk 4702 migration shape, rendered offline (no database)."""

from __future__ import annotations

import importlib.util
import io
import re
from pathlib import Path
from typing import Any

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.models.base import Base
from app.models.binance_trend import (
    INTENT_STATES,
    SIGNAL_STATES,
    BinanceTrendIntent,
    BinanceTrendSignal,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = ROOT / "alembic/versions/20261114_hk4702_trend_state.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("hk4702_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(fn: str) -> str:
    module = _load()
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={
            "as_sql": True,
            "output_buffer": buffer,
            "target_metadata": Base.metadata,
        },
    )
    with Operations.context(context):
        getattr(module, fn)()
    return buffer.getvalue()


def _lines(ddl: str, prefix: str) -> set[str]:
    return {
        line.strip().rstrip(",")
        for line in ddl.splitlines()
        if line.strip().startswith(prefix)
    }


def _columns(ddl: str) -> set[str]:
    skip = ("CONSTRAINT", "CREATE", ")", "")
    return {
        line.strip().split()[0]
        for line in ddl.splitlines()
        if line.strip() and not line.strip().startswith(skip)
    }


def _table(sql: str, table: str) -> str:
    start = sql.index(f"CREATE TABLE review.{table} (")
    return sql[start : sql.index(";", start)]


@pytest.mark.parametrize(
    ("model", "table"),
    [
        (BinanceTrendSignal, "binance_trend_signals"),
        (BinanceTrendIntent, "binance_trend_intents"),
    ],
)
def test_migration_matches_the_orm_model(model: Any, table: str) -> None:
    migration = _table(_render("upgrade"), table)
    orm = str(CreateTable(model.__table__).compile(dialect=postgresql.dialect()))
    assert _columns(migration) == _columns(orm)
    assert _lines(migration, "CONSTRAINT") == _lines(orm, "CONSTRAINT")


def test_the_state_vocabularies_are_closed() -> None:
    assert {"uncertain", "conflict_blocked", "conflict_rejected"} <= set(SIGNAL_STATES)
    assert {"uncertain", "sending"} <= set(INTENT_STATES)


def test_upgrade_is_additive_with_a_conditional_app_grant() -> None:
    sql = _render("upgrade")
    upper = sql.upper()
    assert "DROP TABLE" not in upper and "DROP COLUMN" not in upper
    assert not re.search(r"ALTER TABLE (?!review\.binance_trend_)", upper)
    # Hard rule 18: the Stage 4 role may be absent in dev/CI, and no DELETE.
    grant = sql[sql.index("DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles") :]
    assert "rolname = 'at_app'" in grant
    assert "GRANT SELECT, INSERT, UPDATE ON review.binance_trend_signals" in grant
    assert "review.binance_trend_intents TO at_app" in grant
    assert "DELETE" not in grant.upper() and "TRUNCATE" not in grant.upper()


def test_upgrade_creates_the_append_only_and_never_resend_guards() -> None:
    sql = _render("upgrade")
    for needle in (
        "trg_binance_trend_signals_guard",
        "trg_binance_trend_signals_truncate",
        "trg_binance_trend_intents_guard",
        "trg_binance_trend_intents_truncate",
        "BEFORE INSERT OR UPDATE OR DELETE ON review.binance_trend_signals",
        "BEFORE INSERT OR UPDATE OR DELETE ON review.binance_trend_intents",
        "BEFORE TRUNCATE ON review.binance_trend_intents",
    ):
        assert needle in sql
    # An uncertain intent may only move to an evidence-backed outcome.
    assert "WHEN 'uncertain' THEN NEW.state IN" in sql
    uncertain = sql[sql.index("WHEN 'uncertain' THEN") :].split("ELSE FALSE")[0]
    assert "'reserved'" not in uncertain and "'sending'" not in uncertain


def test_downgrade_refuses_while_rows_exist() -> None:
    sql = _render("downgrade")
    assert "LOCK TABLE review.binance_trend_signals" in sql
    assert "RAISE EXCEPTION" in sql and "restrict_violation" in sql
    assert sql.index("RAISE EXCEPTION") < sql.index("DROP TABLE")
    assert "DROP TABLE review.binance_trend_intents" in sql
    assert sql.index("DROP TABLE review.binance_trend_intents") < sql.index(
        "DROP TABLE review.binance_trend_signals"
    )


def test_bootstrap_loads_the_guard_ddl_from_the_migration() -> None:
    from tests._schema_bootstrap import _DDL_STATEMENTS

    for statement in _load().GUARD_DDL:
        assert statement in _DDL_STATEMENTS
