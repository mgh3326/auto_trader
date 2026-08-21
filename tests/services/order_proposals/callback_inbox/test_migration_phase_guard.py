"""W5 — the migration guard, shown hostile input.

Adversarial review R23. The additive-migration guard only ever looked at
direct ``op.<attr>(...)`` calls across the whole module. That misses:

* phase inversion -- a ``drop_table`` in ``upgrade`` or a ``create_table`` in
  ``downgrade`` -- because it never asked which function a call was in;
* ``o = op`` followed by ``o.execute(...)``;
* ``getattr(op, "execute")(...)``;
* ``op.get_bind().execute(...)`` and any other raw SQL;
* a helper function that holds ``op`` and does the work one call away.

The real migration is additive and clean, so none of this is a live defect --
it is the *guard* that is false green, and a guard nobody has shown a hostile
input to is a guard nobody has tested.
"""

from __future__ import annotations

import pathlib

import pytest

from ._migration_guard import scan

pytestmark = pytest.mark.unit

TABLE = "telegram_callback_inbox"
SCHEMA = "review"


def _scan(source: str):
    return scan(source, table=TABLE, schema=SCHEMA)


#: Each entry is a migration the guard must reject, and the reason it exists.
HOSTILE_CORPUS: tuple[tuple[str, str], ...] = (
    (
        "phase_inversion_drop_in_upgrade",
        """
def upgrade():
    op.create_table("telegram_callback_inbox", schema="review")
    op.drop_table("order_proposals", schema="review")


def downgrade():
    op.drop_index("ix_telegram_callback_inbox_state_available",
                  table_name="telegram_callback_inbox", schema="review")
    op.drop_table("telegram_callback_inbox", schema="review")
""",
    ),
    (
        "phase_inversion_create_in_downgrade",
        """
def upgrade():
    op.create_table("telegram_callback_inbox", schema="review")


def downgrade():
    op.create_table("shadow_copy", schema="review")
    op.drop_table("telegram_callback_inbox", schema="review")
""",
    ),
    (
        "alias_rhs",
        """
def upgrade():
    o = op
    o.execute("UPDATE review.order_proposals SET lifecycle_state = 'proposed'")
    op.create_table("telegram_callback_inbox", schema="review")


def downgrade():
    op.drop_table("telegram_callback_inbox", schema="review")
""",
    ),
    (
        "alias_annassign",
        """
def upgrade():
    o: object = op
    o.bulk_insert(some_table, [{"a": 1}])
    op.create_table("telegram_callback_inbox", schema="review")


def downgrade():
    op.drop_table("telegram_callback_inbox", schema="review")
""",
    ),
    (
        "getattr_indirection",
        """
def upgrade():
    getattr(op, "execute")("DELETE FROM review.order_proposals")
    op.create_table("telegram_callback_inbox", schema="review")


def downgrade():
    op.drop_table("telegram_callback_inbox", schema="review")
""",
    ),
    (
        "get_bind_raw_sql",
        """
def upgrade():
    op.get_bind().execute("TRUNCATE review.order_proposal_rungs")
    op.create_table("telegram_callback_inbox", schema="review")


def downgrade():
    op.drop_table("telegram_callback_inbox", schema="review")
""",
    ),
    (
        "connection_execute",
        """
def upgrade():
    connection = op.get_bind()
    connection.execute(sa.text("UPDATE review.order_proposals SET side = 'buy'"))
    op.create_table("telegram_callback_inbox", schema="review")


def downgrade():
    op.drop_table("telegram_callback_inbox", schema="review")
""",
    ),
    (
        "helper_indirection",
        """
def _really_do_it(operations):
    operations.execute("DELETE FROM review.toss_live_order_ledger")


def upgrade():
    _really_do_it(op)
    op.create_table("telegram_callback_inbox", schema="review")


def downgrade():
    op.drop_table("telegram_callback_inbox", schema="review")
""",
    ),
    (
        "unqualified_schema",
        """
def upgrade():
    op.create_table("telegram_callback_inbox")


def downgrade():
    op.drop_table("telegram_callback_inbox")
""",
    ),
    (
        "wrong_schema",
        """
def upgrade():
    op.create_table("telegram_callback_inbox", schema="public")


def downgrade():
    op.drop_table("telegram_callback_inbox", schema="public")
""",
    ),
    (
        "other_table",
        """
def upgrade():
    op.create_table("telegram_callback_inbox", schema="review")
    op.create_index("ix_evil", "order_proposals", ["symbol"], schema="review")


def downgrade():
    op.drop_table("telegram_callback_inbox", schema="review")
""",
    ),
    (
        "direct_alembic_op_import",
        """
from alembic.op import create_table, execute


def upgrade():
    execute("DELETE FROM review.order_proposals")
    create_table("telegram_callback_inbox", schema="review")


def downgrade():
    op.drop_table("telegram_callback_inbox", schema="review")
""",
    ),
)


@pytest.mark.parametrize(
    ("label", "source"), HOSTILE_CORPUS, ids=[case[0] for case in HOSTILE_CORPUS]
)
def test_the_guard_rejects_every_hostile_migration(label: str, source: str) -> None:
    """R23 — each of these is something the original scanner waved through."""
    result = _scan(source)
    assert not result.ok, f"{label}: the guard accepted a hostile migration"


def test_the_guard_accepts_the_clean_shape() -> None:
    """Anti-vacuity: it must not simply reject everything."""
    clean = """
_TABLE = "telegram_callback_inbox"
_SCHEMA = "review"


def upgrade():
    op.create_table(_TABLE, schema=_SCHEMA)
    op.create_index(
        "ix_telegram_callback_inbox_state_available",
        _TABLE,
        ["state", "available_at"],
        schema=_SCHEMA,
    )


def downgrade():
    op.drop_index(
        "ix_telegram_callback_inbox_state_available",
        table_name=_TABLE,
        schema=_SCHEMA,
    )
    op.drop_table(_TABLE, schema=_SCHEMA)
"""
    result = _scan(clean)
    assert result.ok, result.offenders
    assert result.ops_by_phase["upgrade"] == ["create_table", "create_index"]
    assert result.ops_by_phase["downgrade"] == ["drop_index", "drop_table"]


# ---------------------------------------------------------------------------
# the real migration, through the strengthened guard and at runtime
# ---------------------------------------------------------------------------

_REPO = pathlib.Path(__file__).resolve().parents[4]
_MIGRATION = _REPO / "alembic/versions/20260821_w5_telegram_callback_inbox.py"


def test_the_real_migration_passes_the_strengthened_guard() -> None:
    """R23 — the guard the corpus just proved, pointed at the real file."""
    result = _scan(_MIGRATION.read_text(encoding="utf-8"))
    assert result.ok, result.offenders
    assert result.ops_by_phase["upgrade"] == ["create_table", "create_index"]
    assert result.ops_by_phase["downgrade"] == ["drop_index", "drop_table"]
    assert set(result.ops_by_phase) == {"upgrade", "downgrade"}


class _OperationRecorder:
    """Stands in for ``op`` and records the calls the migration really makes.

    AST analysis says what the source *looks* like. This says what running it
    actually does, including how constants resolve, which is the half a static
    scan cannot see.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        def _record(*args, **kwargs):
            if name == "f":
                # Alembic's naming-convention helper; pass the name through.
                return args[0] if args else None
            self.calls.append((name, args, kwargs))
            return None

        return _record


def _load_migration():
    import importlib.util

    spec = importlib.util.spec_from_file_location("w5_guard_probe", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_real_migration_makes_exactly_the_calls_it_should(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R23 — a runtime call trace with resolved arguments."""
    module = _load_migration()

    upgrade_recorder = _OperationRecorder()
    monkeypatch.setattr(module, "op", upgrade_recorder)
    module.upgrade()

    names = [name for name, _, _ in upgrade_recorder.calls]
    assert names == ["create_table", "create_index"], names

    create_table = upgrade_recorder.calls[0]
    assert create_table[1][0] == TABLE
    assert create_table[2]["schema"] == SCHEMA

    create_index = upgrade_recorder.calls[1]
    assert create_index[1][0] == "ix_telegram_callback_inbox_state_available"
    assert create_index[1][1] == TABLE
    assert create_index[1][2] == ["state", "available_at"]
    assert create_index[2]["schema"] == SCHEMA
    assert create_index[2].get("unique") is False

    downgrade_recorder = _OperationRecorder()
    monkeypatch.setattr(module, "op", downgrade_recorder)
    module.downgrade()

    names = [name for name, _, _ in downgrade_recorder.calls]
    assert names == ["drop_index", "drop_table"], names
    assert downgrade_recorder.calls[0][2]["table_name"] == TABLE
    assert downgrade_recorder.calls[0][2]["schema"] == SCHEMA
    assert downgrade_recorder.calls[1][1][0] == TABLE
    assert downgrade_recorder.calls[1][2]["schema"] == SCHEMA


def test_the_migration_and_the_orm_declare_the_same_constraints() -> None:
    """R23 — exact parity, not a subset.

    The previous expectation was a `<=` subset check *and* was missing
    `handler_marker_order`, so a constraint could disappear from either side
    without anything noticing.
    """
    import re

    import sqlalchemy as sa

    from app.models.telegram_callback_inbox import TelegramCallbackInboxJob

    orm_checks = {
        constraint.name
        for constraint in TelegramCallbackInboxJob.__table__.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    migration_checks = set(
        re.findall(
            r'name=op\.f\("(ck_telegram_callback_inbox_[a-z_]+)"\)',
            _MIGRATION.read_text(encoding="utf-8"),
        )
    )
    assert orm_checks == migration_checks, {
        "orm_only": sorted(orm_checks - migration_checks),
        "migration_only": sorted(migration_checks - orm_checks),
    }
    # And the set is the one this feature actually needs.
    assert orm_checks == {
        "ck_telegram_callback_inbox_action",
        "ck_telegram_callback_inbox_active_reconstructable",
        "ck_telegram_callback_inbox_attempt_count",
        "ck_telegram_callback_inbox_error_class",
        "ck_telegram_callback_inbox_handler_marker_order",
        "ck_telegram_callback_inbox_max_attempts",
        "ck_telegram_callback_inbox_outcome",
        "ck_telegram_callback_inbox_processing_started_at",
        "ck_telegram_callback_inbox_retry_vocabulary",
        "ck_telegram_callback_inbox_state",
        "ck_telegram_callback_inbox_terminal_scrubbed",
        "ck_telegram_callback_inbox_terminal_state_pending",
    }, sorted(orm_checks)
