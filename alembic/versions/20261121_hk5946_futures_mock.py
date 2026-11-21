"""hk 5946 (선물 T3): KIS 국내 선물 모의 주문 원장 + 송신 전 귀속 신호 원장.

Revision ID: 20261121_hk5946_futures_mock
Revises: 20261120_hk5648_mandate_batches
Create Date: 2026-11-21

``review.kis_futures_mock_order_ledger`` 와 ``review.kis_futures_mock_signal_ledger``
(설계 ``docs/design/5939-futures-model.md`` §4)를 추가한다. 추가형이다 — 기존 표·CHECK
는 바뀌지 않고 ``execution_ledger`` 는 건드리지 않는다. 새 ``order_send_intents``
``account_scope`` 값(``kis_futures_mock``)은 문자열이라 마이그레이션이 필요 없다
(그 표에는 scope CHECK 가 없다).

잠금 상수(1계약·명목 50,000,000 KRW·지정가·닫힌 상품)는 CHECK 에도 반복한다.
CHECK 이름은 ORM 의 ``ck_<table>_`` 규칙과 같은 철자를 ``op.f`` 로 고정한다.

``at_app`` 권한(하드룰 18): 역할이 dev/CI 에 없을 수 있어 조건부 GRANT. DELETE 는 주지
않는다. Downgrade 는 행이 있으면 거부한다(원장 손실 방지).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261121_hk5946_futures_mock"
down_revision: str | Sequence[str] | None = "20261120_hk5648_mandate_batches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "review"
_ORDER_TABLE = "kis_futures_mock_order_ledger"
_SIGNAL_TABLE = "kis_futures_mock_signal_ledger"

_STATUSES = (
    "accepted",
    "rejected",
    "pending",
    "partial",
    "filled",
    "cancelled",
    "expired",
    "uncertain",
    "anomaly",
    "requires_manual_review",
)
_SIGNAL_OUTCOME_STATES = ("recorded", "submitted", "suppressed", "failed")

DOWNGRADE_GUARD = f"""
DO $$
BEGIN
    LOCK TABLE {_SCHEMA}.{_ORDER_TABLE}, {_SCHEMA}.{_SIGNAL_TABLE}
        IN ACCESS EXCLUSIVE MODE;
    IF EXISTS (SELECT 1 FROM {_SCHEMA}.{_ORDER_TABLE})
       OR EXISTS (SELECT 1 FROM {_SCHEMA}.{_SIGNAL_TABLE}) THEN
        RAISE EXCEPTION 'futures mock ledgers hold rows; export before downgrading'
            USING ERRCODE = 'restrict_violation';
    END IF;
END;
$$
"""


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.create_table(
        _ORDER_TABLE,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("trade_date", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("trading_day", sa.Text(), nullable=False),
        sa.Column("contract_symbol", sa.Text(), nullable=False),
        sa.Column("broker_symbol", sa.Text(), nullable=False),
        sa.Column("product_code", sa.Text(), nullable=False),
        sa.Column("contract_month", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("order_type", sa.Text(), nullable=False),
        sa.Column("position_effect", sa.Text(), nullable=False),
        sa.Column("operation_kind", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("price", sa.Numeric(20, 4), nullable=False),
        sa.Column("amount", sa.Numeric(24, 4), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("client_order_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("broker_order_id", sa.Text(), nullable=True),
        sa.Column("original_order_id", sa.Text(), nullable=True),
        sa.Column("account_mode", sa.Text(), nullable=False),
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("response_code", sa.Text(), nullable=True),
        sa.Column("response_message", sa.Text(), nullable=True),
        sa.Column(
            "raw_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("filled_qty", sa.Numeric(20, 8), nullable=True),
        sa.Column("avg_fill_price", sa.Numeric(20, 4), nullable=True),
        sa.Column("fee", sa.Numeric(20, 4), nullable=True),
        sa.Column("realized_pnl_krw", sa.Numeric(24, 4), nullable=True),
        sa.Column(
            "reconcile_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("reconciled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("strategy", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
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
            "client_order_id",
            name="uq_kis_futures_mock_order_ledger_client_order_id",
        ),
        sa.UniqueConstraint(
            "account_mode",
            "trading_day",
            "broker_order_id",
            name="uq_kis_futures_mock_order_ledger_account_day_broker_order",
        ),
        sa.CheckConstraint(
            "broker = 'kis'", name=op.f("ck_kis_futures_mock_order_ledger_broker")
        ),
        sa.CheckConstraint(
            "account_mode = 'kis_futures_mock'",
            name=op.f("ck_kis_futures_mock_order_ledger_account_mode"),
        ),
        sa.CheckConstraint(
            _in("product_code", ("MF200",)),
            name=op.f("ck_kis_futures_mock_order_ledger_product"),
        ),
        sa.CheckConstraint(
            "currency = 'KRW'",
            name=op.f("ck_kis_futures_mock_order_ledger_currency"),
        ),
        sa.CheckConstraint(
            "operation_kind IN ('place','cancel')",
            name=op.f("ck_kis_futures_mock_order_ledger_operation_kind"),
        ),
        sa.CheckConstraint(
            "side IN ('buy','sell')",
            name=op.f("ck_kis_futures_mock_order_ledger_side"),
        ),
        sa.CheckConstraint(
            "order_type = 'limit'",
            name=op.f("ck_kis_futures_mock_order_ledger_limit_only"),
        ),
        sa.CheckConstraint(
            "position_effect IN ('open','close')",
            name=op.f("ck_kis_futures_mock_order_ledger_position_effect"),
        ),
        sa.CheckConstraint(
            "quantity > 0 AND quantity <= 1",
            name=op.f("ck_kis_futures_mock_order_ledger_quantity_locked"),
        ),
        sa.CheckConstraint(
            "price > 0",
            name=op.f("ck_kis_futures_mock_order_ledger_price_positive"),
        ),
        sa.CheckConstraint(
            "operation_kind <> 'place' OR amount <= 50000000",
            name=op.f("ck_kis_futures_mock_order_ledger_notional_locked"),
        ),
        sa.CheckConstraint(
            _in("status", _STATUSES),
            name=op.f("ck_kis_futures_mock_order_ledger_status"),
        ),
        sa.CheckConstraint(
            "status <> 'accepted' OR broker_order_id IS NOT NULL",
            name=op.f("ck_kis_futures_mock_order_ledger_accepted_has_broker_order_id"),
        ),
        sa.CheckConstraint(
            "operation_kind = 'place' OR original_order_id IS NOT NULL",
            name=op.f("ck_kis_futures_mock_order_ledger_original_required_for_cancel"),
        ),
        sa.CheckConstraint(
            "length(btrim(client_order_id)) > 0",
            name=op.f("ck_kis_futures_mock_order_ledger_client_id_nonblank"),
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_kis_futures_mock_order_ledger_status",
        _ORDER_TABLE,
        ["status"],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_kis_futures_mock_order_ledger_day_symbol",
        _ORDER_TABLE,
        ["trading_day", "contract_symbol"],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_kis_futures_mock_order_ledger_correlation_id",
        _ORDER_TABLE,
        ["correlation_id"],
        schema=_SCHEMA,
    )

    op.create_table(
        _SIGNAL_TABLE,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("strategy", sa.Text(), nullable=False),
        sa.Column("signal_source", sa.Text(), nullable=False),
        sa.Column("account_mode", sa.Text(), nullable=False),
        sa.Column("contract_symbol", sa.Text(), nullable=False),
        sa.Column("broker_symbol", sa.Text(), nullable=False),
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
            "correlation_id",
            name="uq_kis_futures_mock_signal_ledger_correlation_id",
        ),
        sa.CheckConstraint(
            "account_mode = 'kis_futures_mock'",
            name=op.f("ck_kis_futures_mock_signal_ledger_account_mode"),
        ),
        sa.CheckConstraint(
            "decision IN ('order','no_order')",
            name=op.f("ck_kis_futures_mock_signal_ledger_decision"),
        ),
        sa.CheckConstraint(
            _in("outcome_state", _SIGNAL_OUTCOME_STATES),
            name=op.f("ck_kis_futures_mock_signal_ledger_outcome_state"),
        ),
        sa.CheckConstraint(
            "side IS NULL OR side IN ('buy','sell')",
            name=op.f("ck_kis_futures_mock_signal_ledger_side"),
        ),
        sa.CheckConstraint(
            "length(btrim(strategy)) > 0",
            name=op.f("ck_kis_futures_mock_signal_ledger_strategy_nonblank"),
        ),
        sa.CheckConstraint(
            "length(btrim(signal_source)) > 0",
            name=op.f("ck_kis_futures_mock_signal_ledger_source_nonblank"),
        ),
        sa.CheckConstraint(
            "length(btrim(correlation_id)) > 0",
            name=op.f("ck_kis_futures_mock_signal_ledger_correlation_nonblank"),
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_kis_futures_mock_signal_ledger_kst_date",
        _SIGNAL_TABLE,
        ["kst_date"],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_kis_futures_mock_signal_ledger_strategy",
        _SIGNAL_TABLE,
        ["strategy"],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_kis_futures_mock_signal_ledger_outcome_state",
        _SIGNAL_TABLE,
        ["outcome_state"],
        schema=_SCHEMA,
    )

    # Stage 4 역할은 dev/CI DB 에 없을 수 있다 (하드룰 18).
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
