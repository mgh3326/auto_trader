"""hk 4703 (3797 T-P1): Kiwoom mock order ledger + pre-submit signal ledger.

Revision ID: 20261115_hk4703_kiwoom_ledger
Revises: 20261115_hk4617_flow_revoke
Create Date: 2026-11-15

Adds ``review.kiwoom_mock_order_ledger`` and ``review.kiwoom_mock_signal_ledger``
(design docs/design/3797-kiwoom-mock-ledger.md §2.1 / §2.4) and widens the
``review.order_proposals.account_mode`` CHECK with ``'kiwoom_mock'`` only.

Schema-only: no service, no writer, no send-path wiring (T-P2), no
execution_ledger change (T-P4). ``'kiwoom_mock_us'`` is admitted by the two new
ledger tables' CHECKs (they are the mock account vocabulary of §2.1) but is not
added to ``order_proposals``, which this PR widens with ``'kiwoom_mock'`` only
(design §4.1).

Constraint names: the ORM applies ``ck_%(table_name)s_%(constraint_name)s``
to CHECK names, so the ORM uses short names and this migration pins the
ORM-canonical ``ck_`` spelling with ``op.f``. The ``order_proposals`` CHECK was
created by 20260710_rob816 under the literal name ``order_proposals_account_mode``;
the swap drops both spellings with IF EXISTS (same drift handling as
20260925_task691_toss_expired).

Conditional ``at_app`` grants (hard rule 18): the role may be absent in dev and
CI. Downgrade refuses while either new table holds rows or any proposal is tagged
``'kiwoom_mock'``, because the old CHECK would reject such rows.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261115_hk4703_kiwoom_ledger"
down_revision: str | Sequence[str] | None = "20261115_hk4617_flow_revoke"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "review"
_ORDER_TABLE = "kiwoom_mock_order_ledger"
_SIGNAL_TABLE = "kiwoom_mock_signal_ledger"
_PROPOSAL_TABLE = "order_proposals"
_PROPOSAL_CHECK = "ck_order_proposals_order_proposals_account_mode"
_PROPOSAL_CHECK_NAMES = (_PROPOSAL_CHECK, "order_proposals_account_mode")

_OLD_PROPOSAL_ACCOUNT_MODES = (
    "kis_live",
    "kis_mock",
    "toss_live",
    "upbit",
    "db_simulated",
)
_NEW_PROPOSAL_ACCOUNT_MODES = (*_OLD_PROPOSAL_ACCOUNT_MODES, "kiwoom_mock")

_STATUSES = (
    "accepted",
    "rejected",
    "pending",
    "partial",
    "filled",
    "cancelled",
    "expired",
    "replaced",
    "uncertain",
    "anomaly",
    "requires_manual_review",
)
# Terminal for the open-operation partial unique: a modify/cancel row in any
# other status (including uncertain / anomaly / requires_manual_review) still
# blocks a competing correction or cancel on the same original order.
_TERMINAL_STATUSES = ("rejected", "filled", "cancelled", "expired", "replaced")
_SIGNAL_OUTCOME_STATES = ("recorded", "submitted", "suppressed", "failed")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


_ORDER_ACCOUNT_MARKET_CURRENCY = (
    "(account_mode = 'kiwoom_mock' AND market = 'kr' "
    "AND instrument_type = 'equity_kr' AND currency = 'KRW') "
    "OR (account_mode = 'kiwoom_mock_us' AND market = 'us' "
    "AND instrument_type = 'equity_us' AND currency = 'USD')"
)

_OPEN_OPERATION_WHERE = (
    "operation_kind IN ('modify','cancel') AND status NOT IN ("
    + ", ".join(repr(s) for s in _TERMINAL_STATUSES)
    + ")"
)

DOWNGRADE_GUARD = f"""
DO $$
BEGIN
    LOCK TABLE {_SCHEMA}.{_ORDER_TABLE}, {_SCHEMA}.{_SIGNAL_TABLE}, {_SCHEMA}.{_PROPOSAL_TABLE}
        IN ACCESS EXCLUSIVE MODE;
    IF EXISTS (SELECT 1 FROM {_SCHEMA}.{_ORDER_TABLE})
       OR EXISTS (SELECT 1 FROM {_SCHEMA}.{_SIGNAL_TABLE})
       OR EXISTS (
           SELECT 1 FROM {_SCHEMA}.{_PROPOSAL_TABLE}
           WHERE account_mode = 'kiwoom_mock'
       ) THEN
        RAISE EXCEPTION 'kiwoom mock ledgers hold rows or proposals use kiwoom_mock; export before downgrading'
            USING ERRCODE = 'restrict_violation';
    END IF;
END;
$$
"""


def _drop_proposal_account_mode_check() -> None:
    for name in _PROPOSAL_CHECK_NAMES:
        op.execute(
            f'ALTER TABLE {_SCHEMA}.{_PROPOSAL_TABLE} DROP CONSTRAINT IF EXISTS "{name}"'
        )


def upgrade() -> None:
    op.create_table(
        _ORDER_TABLE,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("trade_date", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("trading_day", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("raw_symbol", sa.Text(), nullable=False),
        sa.Column("instrument_type", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=True),
        sa.Column("order_type", sa.Text(), nullable=True),
        sa.Column("operation_kind", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=True),
        sa.Column("price", sa.Numeric(20, 8), nullable=True),
        sa.Column("amount", sa.Numeric(20, 8), nullable=True),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("client_order_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("approval_hash", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("report_item_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("broker_order_id", sa.Text(), nullable=True),
        sa.Column("original_order_id", sa.Text(), nullable=True),
        sa.Column("account_mode", sa.Text(), nullable=False),
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("lifecycle_state", sa.Text(), nullable=True),
        sa.Column("response_code", sa.Text(), nullable=True),
        sa.Column("response_message", sa.Text(), nullable=True),
        sa.Column(
            "raw_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("filled_qty", sa.Numeric(20, 8), nullable=True),
        sa.Column("avg_fill_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("fee", sa.Numeric(20, 8), nullable=True),
        sa.Column("trade_id", sa.BigInteger(), nullable=True),
        sa.Column("journal_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "reconcile_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "last_reconcile_detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("reconciled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("strategy", sa.Text(), nullable=True),
        sa.Column("thesis", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("exit_intent", sa.Text(), nullable=True),
        sa.Column("exit_reason", sa.Text(), nullable=True),
        sa.Column(
            "indicators_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "client_order_id", name="uq_kiwoom_mock_order_ledger_client_order_id"
        ),
        sa.UniqueConstraint(
            "account_mode",
            "trading_day",
            "broker_order_id",
            name="uq_kiwoom_mock_order_ledger_account_day_broker_order",
        ),
        sa.CheckConstraint(
            "broker = 'kiwoom'", name=op.f("ck_kiwoom_mock_order_ledger_broker")
        ),
        sa.CheckConstraint(
            _in("account_mode", ("kiwoom_mock", "kiwoom_mock_us")),
            name=op.f("ck_kiwoom_mock_order_ledger_account_mode"),
        ),
        sa.CheckConstraint(
            _in("market", ("kr", "us")),
            name=op.f("ck_kiwoom_mock_order_ledger_market"),
        ),
        sa.CheckConstraint(
            _in("instrument_type", ("equity_kr", "equity_us")),
            name=op.f("ck_kiwoom_mock_order_ledger_instrument_type"),
        ),
        sa.CheckConstraint(
            _in("currency", ("KRW", "USD")),
            name=op.f("ck_kiwoom_mock_order_ledger_currency"),
        ),
        sa.CheckConstraint(
            _ORDER_ACCOUNT_MARKET_CURRENCY,
            name=op.f("ck_kiwoom_mock_order_ledger_account_market_currency"),
        ),
        sa.CheckConstraint(
            _in("operation_kind", ("place", "modify", "cancel")),
            name=op.f("ck_kiwoom_mock_order_ledger_operation_kind"),
        ),
        sa.CheckConstraint(
            "side IS NULL OR side IN ('buy','sell')",
            name=op.f("ck_kiwoom_mock_order_ledger_side"),
        ),
        sa.CheckConstraint(
            "order_type IS NULL OR order_type IN ('limit','market')",
            name=op.f("ck_kiwoom_mock_order_ledger_order_type"),
        ),
        sa.CheckConstraint(
            "market <> 'kr' OR order_type IS NULL OR order_type = 'limit'",
            name=op.f("ck_kiwoom_mock_order_ledger_kr_limit_only"),
        ),
        sa.CheckConstraint(
            _in("status", _STATUSES),
            name=op.f("ck_kiwoom_mock_order_ledger_status"),
        ),
        sa.CheckConstraint(
            "status <> 'accepted' OR broker_order_id IS NOT NULL",
            name=op.f("ck_kiwoom_mock_order_ledger_accepted_has_broker_order_id"),
        ),
        # PostgreSQL unique indexes treat NULLs as distinct, so a correction
        # without its original order id would bypass the open-operation index.
        sa.CheckConstraint(
            "operation_kind = 'place' OR original_order_id IS NOT NULL",
            name=op.f("ck_kiwoom_mock_order_ledger_original_required_for_correction"),
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_kiwoom_mock_order_ledger_status",
        _ORDER_TABLE,
        ["status"],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_kiwoom_mock_order_ledger_market_symbol",
        _ORDER_TABLE,
        ["market", "symbol"],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_kiwoom_mock_order_ledger_report_item_uuid",
        _ORDER_TABLE,
        ["report_item_uuid"],
        schema=_SCHEMA,
    )
    op.create_index(
        "uq_kiwoom_mock_order_ledger_open_operation",
        _ORDER_TABLE,
        ["account_mode", "trading_day", "original_order_id"],
        unique=True,
        schema=_SCHEMA,
        postgresql_where=sa.text(_OPEN_OPERATION_WHERE),
    )

    op.create_table(
        _SIGNAL_TABLE,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("strategy", sa.Text(), nullable=False),
        sa.Column("signal_source", sa.Text(), nullable=False),
        sa.Column("account_mode", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("instrument_type", sa.Text(), nullable=True),
        sa.Column("side", sa.Text(), nullable=True),
        sa.Column("intended_quantity", sa.Numeric(20, 8), nullable=True),
        sa.Column("intended_price", sa.Numeric(20, 4), nullable=True),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column(
            "outcome_state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'recorded'"),
        ),
        sa.Column("suppressed_reason", sa.Text(), nullable=True),
        sa.Column("report_item_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("kst_date", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "correlation_id", name="uq_kiwoom_mock_signal_ledger_correlation_id"
        ),
        sa.CheckConstraint(
            _in("account_mode", ("kiwoom_mock", "kiwoom_mock_us")),
            name=op.f("ck_kiwoom_mock_signal_ledger_account_mode"),
        ),
        sa.CheckConstraint(
            "decision IN ('order','no_order')",
            name=op.f("ck_kiwoom_mock_signal_ledger_decision"),
        ),
        sa.CheckConstraint(
            _in("outcome_state", _SIGNAL_OUTCOME_STATES),
            name=op.f("ck_kiwoom_mock_signal_ledger_outcome_state"),
        ),
        sa.CheckConstraint(
            "side IS NULL OR side IN ('buy','sell')",
            name=op.f("ck_kiwoom_mock_signal_ledger_side"),
        ),
        sa.CheckConstraint(
            "length(btrim(strategy)) > 0",
            name=op.f("ck_kiwoom_mock_signal_ledger_strategy_nonblank"),
        ),
        sa.CheckConstraint(
            "length(btrim(signal_source)) > 0",
            name=op.f("ck_kiwoom_mock_signal_ledger_source_nonblank"),
        ),
        sa.CheckConstraint(
            "length(btrim(correlation_id)) > 0",
            name=op.f("ck_kiwoom_mock_signal_ledger_correlation_nonblank"),
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_kiwoom_mock_signal_ledger_kst_date",
        _SIGNAL_TABLE,
        ["kst_date"],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_kiwoom_mock_signal_ledger_strategy",
        _SIGNAL_TABLE,
        ["strategy"],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_kiwoom_mock_signal_ledger_symbol",
        _SIGNAL_TABLE,
        ["symbol"],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_kiwoom_mock_signal_ledger_outcome_state",
        _SIGNAL_TABLE,
        ["outcome_state"],
        schema=_SCHEMA,
    )

    _drop_proposal_account_mode_check()
    op.create_check_constraint(
        op.f(_PROPOSAL_CHECK),
        _PROPOSAL_TABLE,
        _in("account_mode", _NEW_PROPOSAL_ACCOUNT_MODES),
        schema=_SCHEMA,
    )

    # The Stage 4 role may not exist in dev and CI databases.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') "
        "THEN GRANT USAGE ON SCHEMA review TO at_app; "
        f"GRANT SELECT, INSERT, UPDATE ON {_SCHEMA}.{_ORDER_TABLE} TO at_app; "
        f"GRANT USAGE, SELECT ON SEQUENCE {_SCHEMA}.{_ORDER_TABLE}_id_seq TO at_app; "
        f"GRANT SELECT, INSERT, UPDATE ON {_SCHEMA}.{_SIGNAL_TABLE} TO at_app; "
        f"GRANT USAGE, SELECT ON SEQUENCE {_SCHEMA}.{_SIGNAL_TABLE}_id_seq TO at_app; "
        "END IF; END $$"
    )


def downgrade() -> None:
    op.execute(DOWNGRADE_GUARD)
    op.drop_table(_SIGNAL_TABLE, schema=_SCHEMA)
    op.drop_table(_ORDER_TABLE, schema=_SCHEMA)
    _drop_proposal_account_mode_check()
    op.create_check_constraint(
        op.f(_PROPOSAL_CHECK),
        _PROPOSAL_TABLE,
        _in("account_mode", _OLD_PROPOSAL_ACCOUNT_MODES),
        schema=_SCHEMA,
    )
