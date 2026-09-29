"""Typed read model of the NHPLUG mock dispatch tables.

The explicit Alembic migration owns DDL and trigger policy. This module is
imported by readers only; normal ledger writes go through NHPlugMockLedger.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    Date,
    Identity,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class NHPlugMockDispatchBase(DeclarativeBase):
    """Separate metadata: the explicit migration, not create_all, owns DDL."""


class NHPlugMockKeyVersion(NHPlugMockDispatchBase):
    __tablename__ = "nhplug_mock_key_version"
    __table_args__ = {"schema": "review"}

    key_version: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    key_id: Mapped[str] = mapped_column(Text, nullable=False)
    key_check: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )


class NHPlugMockAccountRef(NHPlugMockDispatchBase):
    __tablename__ = "nhplug_mock_account_ref"
    __table_args__ = {"schema": "review"}

    account_ref: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )


class NHPlugMockAccountBinding(NHPlugMockDispatchBase):
    __tablename__ = "nhplug_mock_account_binding"
    __table_args__ = {"schema": "review"}

    key_version: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    binding: Mapped[str] = mapped_column(Text, primary_key=True)
    account_ref: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )


class NHPlugSuccessProofCode(NHPlugMockDispatchBase):
    __tablename__ = "nhplug_success_proof_code"
    __table_args__ = {"schema": "review"}

    path: Mapped[str] = mapped_column(Text, primary_key=True)
    rsp_cd: Mapped[str] = mapped_column(Text, primary_key=True)
    citation: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )


class NHPlugNoOrderProofCode(NHPlugMockDispatchBase):
    __tablename__ = "nhplug_no_order_proof_code"
    __table_args__ = {"schema": "review"}

    path: Mapped[str] = mapped_column(Text, primary_key=True)
    rsp_cd: Mapped[str] = mapped_column(Text, primary_key=True)
    citation: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )


class NHPlugMockOperatorAuthorization(NHPlugMockDispatchBase):
    __tablename__ = "nhplug_mock_operator_authorization"
    __table_args__ = {"schema": "review"}

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_row_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    account_ref: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    order_date: Mapped[date] = mapped_column(Date, nullable=False)
    body_digest: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_order_id: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)
    dispatcher_gone_proof: Mapped[bool] = mapped_column(Boolean, nullable=False)
    grace_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    operator_id: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    consumed_by_row_id: Mapped[int | None] = mapped_column(BigInteger)
    consumed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class NHPlugMockOrderLedger(NHPlugMockDispatchBase):
    __tablename__ = "nhplug_mock_order_ledger"
    __table_args__ = {"schema": "review"}

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    client_request_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    account_ref: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    order_date: Mapped[date] = mapped_column(Date, nullable=False)
    operation_kind: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[int | None] = mapped_column(BigInteger)
    price: Mapped[int | None] = mapped_column(BigInteger)
    original_order_id: Mapped[str | None] = mapped_column(Text)
    amend_scope: Mapped[str | None] = mapped_column(Text)
    body_schema_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    body_digest: Mapped[str] = mapped_column(
        Text,
        Computed(
            "review.nhplug_body_digest_v1(operation_kind,side,symbol,quantity,price,"
            "original_order_id,amend_scope,account_ref::text)",
            persisted=True,
        ),
        nullable=False,
    )
    duplicate_of: Mapped[int | None] = mapped_column(BigInteger)
    duplicate_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    second_order_authorization_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True)
    )
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'intent'")
    )
    claim_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    claimed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    claim_deadline: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    lease_machine_id: Mapped[str | None] = mapped_column(Text)
    lease_boot_id: Mapped[str | None] = mapped_column(Text)
    lease_pid_ns: Mapped[str | None] = mapped_column(Text)
    lease_pid: Mapped[int | None] = mapped_column(Integer)
    lease_process_start: Mapped[int | None] = mapped_column(BigInteger)
    sending_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    lease_closed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    dispatcher_done_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    withdraw_reason: Mapped[str | None] = mapped_column(Text)
    uncertain_reason: Mapped[str | None] = mapped_column(Text)
    broker_order_id: Mapped[str | None] = mapped_column(Text)
    ack_order_id: Mapped[str | None] = mapped_column(Text)
    ack_evidence_order_id: Mapped[str | None] = mapped_column(Text)
    late_result_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    ack_source: Mapped[str | None] = mapped_column(Text)
    success_rsp_cd: Mapped[str | None] = mapped_column(Text)
    reject_rsp_cd: Mapped[str | None] = mapped_column(Text)
    resolution_authorization_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True)
    )
    candidate_order_ids: Mapped[dict | None] = mapped_column(JSONB)
    reconcile_state: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict | None] = mapped_column(JSONB)
    last_reconcile: Mapped[dict | None] = mapped_column(JSONB)
    requires_manual_review: Mapped[bool] = mapped_column(Boolean, nullable=False)
    manual_review_reason: Mapped[str | None] = mapped_column(Text)
    filled_qty: Mapped[int | None] = mapped_column(BigInteger)
    open_qty: Mapped[int | None] = mapped_column(BigInteger)
    cancelled_qty: Mapped[int | None] = mapped_column(BigInteger)
    modified_qty: Mapped[int | None] = mapped_column(BigInteger)
    avg_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 3))
    successor_order_id: Mapped[str | None] = mapped_column(Text)
    applied_qty: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
