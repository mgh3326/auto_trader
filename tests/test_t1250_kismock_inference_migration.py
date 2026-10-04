"""#1250 — static contract for the kis_mock inference-expiry audit migration.

The live proof (upgrade -> downgrade -> upgrade against a throwaway
TimescaleDB PostgreSQL, schema dump identical after downgrade) is recorded in
docs/runbooks/kis-mock-expired-inference-q46.md. CI builds the test schema with
create_all plus tests/_schema_bootstrap.py, so this file pins that the three
copies (migration, ORM model, test bootstrap) say the same thing.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest

from app.models import review as model
from app.services.kis_mock_inference_expiry import ALLOWED_LEDGER_IDS
from tests import _schema_bootstrap as bootstrap

pytestmark = pytest.mark.unit

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "20261005_t1250_kismock_inference.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("_t1250_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def test_revision_chain() -> None:
    migration = _load_migration()
    assert migration.revision == "20261005_t1250_kismock_inf"
    assert migration.down_revision == "20261001_t1175_ledger_quar"
    assert len(migration.revision) <= 32


def test_allowlist_is_the_same_in_rule_model_and_migration() -> None:
    migration = _load_migration()
    assert migration.LEDGER_IDS_SQL == model.KIS_MOCK_INFERENCE_EXPIRY_LEDGER_IDS_SQL
    ids = {int(x) for x in re.findall(r"\d+", migration.LEDGER_IDS_SQL)}
    assert ids == set(ALLOWED_LEDGER_IDS) == {63, 64, 66, 80}


def test_model_checks_match_the_migration() -> None:
    table = model.KISMockInferenceExpiryEvent.__table__
    model_checks = {
        str(c.name): _norm(str(c.sqltext))
        for c in table.constraints
        if c.__class__.__name__ == "CheckConstraint"
    }
    text = MIGRATION.read_text("utf-8")
    for name, sql in model_checks.items():
        assert name in text, name
        assert sql in _norm(text), sql


def test_bootstrap_mirrors_the_migration_triggers() -> None:
    migration = _load_migration()
    ddl = [_norm(s) for s in bootstrap._DDL_STATEMENTS]
    assert _norm(migration.AUDIT_REJECT_FUNCTION_DDL) in ddl
    joined = "\n".join(ddl)
    for trigger in (
        "trg_kis_mock_inference_expiry_events_append_only",
        "trg_kis_mock_inference_expiry_events_truncate",
    ):
        assert f"CREATE TRIGGER {trigger} " in joined
        assert trigger in MIGRATION.read_text("utf-8")


def _op_calls(function: str) -> list[ast.Call]:
    tree = ast.parse(MIGRATION.read_text("utf-8"))
    [node] = [
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function
    ]
    return [
        n
        for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "op"
    ]


def test_upgrade_only_creates_and_never_touches_ledger_data() -> None:
    names = [call.func.attr for call in _op_calls("upgrade")]  # type: ignore[attr-defined]
    assert set(names) <= {"create_table", "create_index", "execute", "f"}
    text = MIGRATION.read_text("utf-8").upper()
    assert "KIS_MOCK_ORDER_LEDGER" not in text
    assert "UPDATE REVIEW." not in text
    assert "DELETE FROM" not in text
    assert "TRUNCATE REVIEW" not in text


def test_downgrade_reverses_everything_upgrade_creates() -> None:
    text = MIGRATION.read_text("utf-8")
    down = text[text.index("def downgrade") :]
    for name in (
        "trg_kis_mock_inference_expiry_events_truncate",
        "trg_kis_mock_inference_expiry_events_append_only",
        "reject_kis_mock_inference_expiry_event_mutation",
        "ix_kis_mock_inference_expiry_events_batch",
        'drop_table("kis_mock_inference_expiry_events"',
    ):
        assert name in down, name


def test_bootstrap_version_was_bumped() -> None:
    assert bootstrap.SCHEMA_BOOTSTRAP_VERSION >= 57
