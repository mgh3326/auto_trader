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
