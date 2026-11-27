"""v3 execution intents bound to a daily_plan row (hk 6991, design 6939 N2).

``review.execution_intents`` gains ``schema_version`` 3: a v1-shaped kis_mock
KRX intent carrying its binding ``(plan_head_id, plan_version, row_key)`` and
the copies the gateway checks (plan account/venue/session/action, entry
invalidation price). Additive: new nullable columns, CHECK swaps that admit v3
and keep v1/v2 exactly, and an index for the per-row lock identity. There is no
foreign key to ``review.daily_plan_heads``: heads are append-only, and the
issuer reads the head before it stores the intent.

The journal guard ``review.execution_child_requests_guard`` is replaced by the
hk 5441 body with one change: ``not_sent`` is accepted for ``schema_version`` 2
with the v2 closed list (unchanged) **and** for ``schema_version`` 3 with the v3
closed list ``plan_version_stale`` / ``prior_intent_unsettled``. Every other
rule of the function is the same text.

No new table, so no new grants; the conditional SELECT on the daily_plan tables
is re-stated because the gateway process now reads them.

Downgrade refuses while any v3 intent exists, then restores the hk 5441 guard
and CHECKs and drops the columns.
"""

# expand-contract: expand-step 6991

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from alembic import op

revision: str = "20261127_hk6991_intent_v3"
down_revision: str | Sequence[str] | None = "20261127_hk6981_plan_widen"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Equal to ``plan_binding.V3_NOT_SENT_REASONS``; a test pins the two.
V3_NOT_SENT_REASONS: tuple[str, ...] = ("plan_version_stale", "prior_intent_unsettled")


def _hk5441() -> Any:
    path = Path(__file__).with_name("20261120_hk5441_upbit_intent_gateway.py")
    spec = importlib.util.spec_from_file_location("hk5441_for_hk6991", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_PREVIOUS = _hk5441()

INTENT_ACCOUNT_BY_VERSION_SQL = (
    "(schema_version = 1 AND account = 'kis_mock' AND venue = 'krx') "
    "OR (schema_version = 2 AND account = 'upbit_live' AND venue = 'upbit') "
    "OR (schema_version = 3 AND account = 'kis_mock' AND venue = 'krx')"
)
INTENT_V3_SHAPE_SQL = (
    "schema_version <> 3 OR ("
    "max_quantity IS NOT NULL AND max_quantity BETWEEN 1 AND 1000000 "
    "AND max_notional_krw BETWEEN 1 AND 5000000 "
    "AND limit_price_krw BETWEEN 1 AND 10000000 "
    "AND position_at_authorization IS NOT NULL "
    "AND min_position_after IS NOT NULL AND max_position_after IS NOT NULL "
    "AND position_at_authorization >= 0 AND min_position_after >= 0 "
    "AND min_position_after <= max_position_after "
    "AND mandate_id IS NULL AND sleeve_id IS NULL AND execution_mode IS NULL "
    "AND strategy_code_sha IS NULL AND decision_ref IS NULL AND origin IS NULL "
    "AND operator_exit_id IS NULL AND max_quantity_decimal IS NULL "
    "AND position_at_authorization_decimal IS NULL "
    "AND min_position_after_decimal IS NULL "
    "AND max_position_after_decimal IS NULL "
    "AND plan_head_id IS NOT NULL AND plan_version IS NOT NULL "
    "AND plan_version > 0 AND plan_row_key IS NOT NULL "
    "AND plan_account_ref IS NOT NULL AND plan_venue IS NOT NULL "
    "AND plan_venue = 'KRX' AND plan_session IS NOT NULL "
    "AND plan_action IS NOT NULL AND plan_action IN ('new', 'add', 'trim') "
    "AND (plan_action = 'trim') = (side = 'sell') "
    "AND (plan_action = 'trim') = (invalidation_price_krw IS NULL) "
    "AND (invalidation_price_krw IS NULL "
    "OR invalidation_price_krw BETWEEN 1 AND limit_price_krw - 1))"
)
INTENT_PLAN_COLUMNS_V3_ONLY_SQL = (
    "schema_version = 3 OR (plan_head_id IS NULL AND plan_version IS NULL "
    "AND plan_row_key IS NULL AND plan_account_ref IS NULL "
    "AND plan_venue IS NULL AND plan_session IS NULL AND plan_action IS NULL "
    "AND invalidation_price_krw IS NULL)"
)

_PLAN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("plan_head_id", "UUID"),
    ("plan_version", "INTEGER"),
    ("plan_row_key", "TEXT"),
    ("plan_account_ref", "TEXT"),
    ("plan_venue", "TEXT"),
    ("plan_session", "TEXT"),
    ("plan_action", "TEXT"),
    ("invalidation_price_krw", "BIGINT"),
)

_swap_check = _PREVIOUS._swap_check

ALTER_DDL: tuple[str, ...] = (
    *(
        f"ALTER TABLE review.execution_intents ADD COLUMN IF NOT EXISTS {name} {kind}"
        for name, kind in _PLAN_COLUMNS
    ),
    "CREATE INDEX IF NOT EXISTS ix_execution_intents_plan_lock "
    "ON review.execution_intents (plan_account_ref, symbol, plan_venue)",
    *_swap_check(
        "execution_intents",
        "ck_execution_intents_schema_version",
        "schema_version IN (1, 2, 3)",
    ),
    *_swap_check(
        "execution_intents",
        "ck_execution_intents_account_by_version",
        INTENT_ACCOUNT_BY_VERSION_SQL,
    ),
    *_swap_check(
        "execution_intents", "ck_execution_intents_v3_shape", INTENT_V3_SHAPE_SQL
    ),
    *_swap_check(
        "execution_intents",
        "ck_execution_intents_plan_columns_v3_only",
        INTENT_PLAN_COLUMNS_V3_ONLY_SQL,
    ),
)

_V2_NOT_SENT_CLAUSE = "IF intent_version IS DISTINCT FROM 2\n"
_V2_NOT_SENT_LIST = f"OR NEW.refusal_reason NOT IN ({_PREVIOUS._NOT_SENT_SQL}) THEN"
_V3_SQL = ", ".join(f"'{reason}'" for reason in V3_NOT_SENT_REASONS)


def _v3_child_guard(previous: str) -> str:
    """The hk 5441 child guard with v3 admitted to ``not_sent`` (its own list)."""
    for fragment in (_V2_NOT_SENT_CLAUSE, _V2_NOT_SENT_LIST):
        if previous.count(fragment) != 1:
            raise RuntimeError(f"hk 5441 child guard changed: {fragment!r}")
    guard = previous.replace(
        _V2_NOT_SENT_CLAUSE,
        "IF intent_version IS NULL OR intent_version NOT IN (2, 3)\n",
    )
    return guard.replace(
        _V2_NOT_SENT_LIST,
        "OR (intent_version = 2 AND NEW.refusal_reason NOT IN "
        f"({_PREVIOUS._NOT_SENT_SQL}))\n"
        f"               OR (intent_version = 3 AND NEW.refusal_reason NOT IN "
        f"({_V3_SQL})) THEN",
    )


_PREVIOUS_CHILD_GUARD: str = next(
    statement
    for statement in _PREVIOUS.GUARD_DDL
    if "FUNCTION review.execution_child_requests_guard()" in statement
)

#: One statement per entry. The test schema bootstrap installs these too.
GUARD_DDL: tuple[str, ...] = (_v3_child_guard(_PREVIOUS_CHILD_GUARD),)

GRANT_DDL: str = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') "
    "THEN GRANT USAGE ON SCHEMA review TO at_app; "
    "GRANT SELECT ON review.daily_plan_heads, review.daily_plan_versions, "
    "review.daily_plan_rows TO at_app; "
    "END IF; END $$"
)

DOWNGRADE_GUARD: str = """
    DO $$
    BEGIN
        LOCK TABLE review.execution_child_requests, review.execution_intents
            IN ACCESS EXCLUSIVE MODE;
        IF EXISTS (SELECT 1 FROM review.execution_intents WHERE schema_version = 3) THEN
            RAISE EXCEPTION 'v3 execution intents exist; export and clear them before downgrading'
                USING ERRCODE = 'restrict_violation';
        END IF;
    END;
    $$
"""

_DOWNGRADE_DDL: tuple[str, ...] = (
    _PREVIOUS_CHILD_GUARD,
    "ALTER TABLE review.execution_intents "
    "DROP CONSTRAINT IF EXISTS ck_execution_intents_plan_columns_v3_only",
    "ALTER TABLE review.execution_intents "
    "DROP CONSTRAINT IF EXISTS ck_execution_intents_v3_shape",
    *_swap_check(
        "execution_intents",
        "ck_execution_intents_account_by_version",
        _PREVIOUS.INTENT_ACCOUNT_BY_VERSION_SQL,
    ),
    *_swap_check(
        "execution_intents",
        "ck_execution_intents_schema_version",
        "schema_version IN (1, 2)",
    ),
    "DROP INDEX IF EXISTS review.ix_execution_intents_plan_lock",
    *(
        f"ALTER TABLE review.execution_intents DROP COLUMN IF EXISTS {name}"
        for name, _ in _PLAN_COLUMNS
    ),
)


def upgrade() -> None:
    for statement in (*ALTER_DDL, *GUARD_DDL):
        op.execute(statement)
    op.execute(GRANT_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_GUARD)
    for statement in _DOWNGRADE_DDL:
        op.execute(statement)
