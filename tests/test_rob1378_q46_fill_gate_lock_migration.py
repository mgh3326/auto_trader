"""ROB-1378 — static contract for the Q-46 fill-gate lock migration.

The behaviour (a fill in flight while the close COMMITs is waited for and then
refuses the close) is proven against a throwaway PostgreSQL in
tests/services/test_kis_mock_inference_expiry_db.py (``test_rob1378_*``). CI
builds the test schema with create_all plus tests/_schema_bootstrap.py, so this
file pins that the migration and the bootstrap say the same thing and that the
replaced #1250 function changed only by the lock section.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest

from tests import _schema_bootstrap as bootstrap

pytestmark = pytest.mark.unit

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
MIGRATION = VERSIONS / "20261016_rob1378_q46_fill_gate_lock.py"
T1250 = VERSIONS / "20261005_t1250_kismock_inference.py"

KEY_SQL = "hashtextextended('auto_trader:kis_mock_q46_fill_gate', 0)"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


@pytest.fixture(scope="module")
def migration():
    return _load(MIGRATION, "_rob1378_migration")


@pytest.fixture(scope="module")
def t1250():
    return _load(T1250, "_t1250_migration_for_rob1378")


def test_revision_chain(migration) -> None:
    assert migration.revision == "20261016_rob1378_q46_lock"
    assert migration.down_revision == "20261005_t1250_kismock_inf"
    assert len(migration.revision) <= 32


def test_one_key_exclusive_on_the_close_shared_on_writers(migration) -> None:
    assert KEY_SQL == f"hashtextextended('{migration.FILL_GATE_LOCK_KEY_TEXT}', 0)"
    close = _norm(migration.REQUIRE_AUDIT_FUNCTION_DDL)
    writer = _norm(migration.FILL_WRITER_LOCK_FUNCTION_DDL)
    assert close.count(f"PERFORM pg_advisory_xact_lock({KEY_SQL});") == 1
    assert "pg_advisory_xact_lock_shared" not in close
    assert writer.count(f"PERFORM pg_advisory_xact_lock_shared( {KEY_SQL});") == 1
    assert "pg_try_advisory" not in close + writer
    assert "RETURN NEW;" in writer and "RAISE" not in writer


def test_close_takes_the_lock_before_every_fill_check(migration) -> None:
    body = migration.REQUIRE_AUDIT_FUNCTION_DDL
    lock = body.index("PERFORM pg_advisory_xact_lock(")
    isolation = body.index("current_setting('transaction_isolation')")
    first_fill_read = body.index("FROM review.execution_ledger x")
    assert isolation < lock < first_fill_read
    assert body.count("FROM review.execution_ledger x") == 2
    assert body.index("FROM review.kis_mock_order_ledger s") > lock


def test_close_body_changed_only_by_the_lock_section(migration, t1250) -> None:
    old = t1250.REQUIRE_AUDIT_FUNCTION_DDL
    new = migration.REQUIRE_AUDIT_FUNCTION_DDL
    old_head, old_tail = old.split("    -- Last fill gate, evaluated at COMMIT", 1)
    new_head, new_tail = new.split("    -- Last fill gate, evaluated at COMMIT", 1)
    assert new_head == old_head
    old_rest = old_tail[old_tail.index("    IF EXISTS (") :]
    new_rest = new_tail[new_tail.index("    IF EXISTS (") :]
    assert new_rest == old_rest


def test_writer_triggers_cover_what_the_gate_reads(migration) -> None:
    exec_trigger = _norm(migration.EXECUTION_LEDGER_LOCK_TRIGGER_DDL)
    mock_trigger = _norm(migration.MOCK_LEDGER_LOCK_TRIGGER_DDL)
    gate = _norm(migration.REQUIRE_AUDIT_FUNCTION_DDL)
    # execution_ledger: the gate reads every kis/mock row (any source,
    # quarantined included); the trigger fires for exactly those rows.
    assert "x.broker = 'kis' AND x.account_mode = 'mock'" in gate
    assert "WHEN (NEW.broker = 'kis' AND NEW.account_mode = 'mock')" in exec_trigger
    assert "BEFORE INSERT OR UPDATE ON review.execution_ledger" in exec_trigger
    # kis_mock_order_ledger: the gate reads siblings by fill state / fill detail
    # and by symbol, correlation and dates; the trigger fires for every row
    # write, so no predicate can drift from the gate's.
    assert "review.kis_mock_q46_detail_has_fill(s.last_reconcile_detail)" in gate
    assert "BEFORE INSERT OR UPDATE ON review.kis_mock_order_ledger" in mock_trigger
    assert "WHEN" not in mock_trigger
    for trigger in (exec_trigger, mock_trigger):
        assert "FOR EACH ROW" in trigger
        assert "EXECUTE FUNCTION review.kis_mock_q46_fill_writer_lock()" in trigger


def test_no_user_defined_function_on_the_ordinary_write_path(migration) -> None:
    # #789 Stage 4: functions created by at_migration_owner carry no PUBLIC
    # EXECUTE. A WHEN is evaluated with the writer's privileges, so it must not
    # call a review.* function; the trigger function body only calls built-ins.
    for trigger in (
        migration.EXECUTION_LEDGER_LOCK_TRIGGER_DDL,
        migration.MOCK_LEDGER_LOCK_TRIGGER_DDL,
    ):
        when = trigger.split("EXECUTE FUNCTION", 1)[0]
        assert "review." not in when.split(" ON review.", 1)[1].split("\n", 1)[1]
    body = migration.FILL_WRITER_LOCK_FUNCTION_DDL.split("$$")[1]
    assert re.findall(r"review\.\w+\(", body) == []


def test_the_close_helper_is_granted_to_at_app(migration) -> None:
    grant = _norm(migration.HELPER_GRANT_DDL)
    assert "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app')" in grant
    assert (
        "GRANT EXECUTE ON FUNCTION review.kis_mock_q46_detail_has_fill(jsonb) "
        "TO at_app"
    ) in grant
    text = MIGRATION.read_text("utf-8")
    up = text[text.index("def upgrade") : text.index("def downgrade")]
    down = text[text.index("def downgrade") :]
    assert "op.execute(HELPER_GRANT_DDL)" in up
    assert "op.execute(HELPER_REVOKE_DDL)" in down
    assert "FROM at_app" in migration.HELPER_REVOKE_DDL
    # The grant is EXECUTE on one pure helper only — nothing on the trigger
    # functions, tables or other roles.
    assert re.findall(r"GRANT (\w+)", text) == ["EXECUTE"]


def test_bootstrap_mirrors_the_head_bodies(migration, t1250) -> None:
    ddl = [_norm(s) for s in bootstrap._DDL_STATEMENTS]
    for statement in (
        migration.REQUIRE_AUDIT_FUNCTION_DDL,
        migration.FILL_WRITER_LOCK_FUNCTION_DDL,
        migration.EXECUTION_LEDGER_LOCK_TRIGGER_DDL,
        migration.MOCK_LEDGER_LOCK_TRIGGER_DDL,
    ):
        assert ddl.count(_norm(statement)) == 1
    # Only the head body: the superseded #1250 one must not be re-applied.
    assert _norm(t1250.REQUIRE_AUDIT_FUNCTION_DDL) not in ddl
    # The function exists before the constraint trigger that names it.
    function_at = ddl.index(_norm(migration.REQUIRE_AUDIT_FUNCTION_DDL))
    trigger_at = ddl.index(_norm(t1250.REQUIRE_AUDIT_TRIGGER_DDL))
    assert function_at < trigger_at
    writer_at = ddl.index(_norm(migration.FILL_WRITER_LOCK_FUNCTION_DDL))
    assert writer_at < ddl.index(_norm(migration.EXECUTION_LEDGER_LOCK_TRIGGER_DDL))
    assert writer_at < ddl.index(_norm(migration.MOCK_LEDGER_LOCK_TRIGGER_DDL))
    assert bootstrap.SCHEMA_BOOTSTRAP_VERSION >= 61


def _op_calls(function: str) -> list[str]:
    tree = ast.parse(MIGRATION.read_text("utf-8"))
    [node] = [
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function
    ]
    return [
        n.func.attr
        for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "op"
    ]


def test_upgrade_never_touches_ledger_data() -> None:
    assert set(_op_calls("upgrade")) == {"execute"}
    text = MIGRATION.read_text("utf-8").upper()
    assert "UPDATE REVIEW." not in text
    assert "INSERT INTO" not in text
    assert "DELETE FROM" not in text
    assert "TRUNCATE" not in text
    assert "ALTER TABLE" not in text


def test_downgrade_drops_the_writer_side_and_restores_the_1250_body(
    migration, t1250
) -> None:
    text = MIGRATION.read_text("utf-8")
    down = text[text.index("def downgrade") :]
    for name in (
        "trg_kis_mock_order_ledger_q46_fill_lock",
        "trg_execution_ledger_kis_mock_q46_fill_lock",
        "DROP FUNCTION IF EXISTS review.kis_mock_q46_fill_writer_lock()",
        "_t1250_require_audit_ddl()",
    ):
        assert name in down, name
    assert migration._t1250_require_audit_ddl() == t1250.REQUIRE_AUDIT_FUNCTION_DDL
