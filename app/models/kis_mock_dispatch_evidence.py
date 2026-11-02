"""Append-only KR KIS mock lineage and evidence; service-owned writes only."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class KISMockDispatchLineage(Base):
    __tablename__ = "kis_mock_dispatch_lineage"
    __table_args__ = (
        UniqueConstraint(
            "claim_account_scope",
            "idempotency_key",
            name="uq_kis_mock_dispatch_scope_key",
        ),
        CheckConstraint("lane_id = 'kr.kis.mock'", name="lane_kr_kis_mock"),
        CheckConstraint(
            "physical_account_id ~ '^kismock:v1:[0-9a-f]{64}$'", name="mock_fingerprint"
        ),
        {"schema": "review"},
    )

    order_attempt_id: Mapped[str] = mapped_column(Text, primary_key=True)
    lane_id: Mapped[str] = mapped_column(Text, nullable=False)
    physical_account_id: Mapped[str] = mapped_column(Text, nullable=False)
    claim_account_scope: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    initial_envelope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class KISMockDispatchEvidence(Base):
    __tablename__ = "kis_mock_dispatch_evidence"
    __table_args__ = (
        UniqueConstraint(
            "order_attempt_id",
            "kind",
            "broker_evidence_key",
            name="uq_kis_mock_dispatch_evidence_key",
        ),
        CheckConstraint(
            "kind IN ('ack','unknown','reject','expiry','partial_fill','cancel','terminal_reconciliation')",
            name="evidence_kind",
        ),
        CheckConstraint(
            "broker_evidence_key ~ '^[0-9a-f]{64}$'", name="evidence_key_digest"
        ),
        CheckConstraint(
            "kind IN ('unknown','reject') OR (broker_order_id IS NOT NULL AND broker_order_id ~ '^[0-9]+$')",
            name="exact_odno_required",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_attempt_id: Mapped[str] = mapped_column(
        ForeignKey("review.kis_mock_dispatch_lineage.order_attempt_id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    broker_evidence_key: Mapped[str] = mapped_column(Text, nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
