"""Investment stage runs/artifacts ORM (ROB-279)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class InvestmentStageRun(Base):
    __tablename__ = "investment_stage_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','completed','failed','blocked')",
            name="ck_investment_stage_runs_status",
        ),
        Index("ix_investment_stage_runs_bundle_uuid", "snapshot_bundle_uuid"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        unique=True,
        server_default=text("gen_random_uuid()"),
    )
    snapshot_bundle_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    market: Mapped[str] = mapped_column(Text, nullable=False)
    market_session: Mapped[str | None] = mapped_column(Text, nullable=True)
    account_scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_version: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'v1'")
    )
    generator_version: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'v1'")
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'running'")
    )
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    artifacts: Mapped[list[InvestmentStageArtifact]] = relationship(
        "InvestmentStageArtifact",
        primaryjoin="InvestmentStageRun.run_uuid==foreign(InvestmentStageArtifact.run_uuid)",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class InvestmentStageArtifact(Base):
    __tablename__ = "investment_stage_artifacts"
    __table_args__ = (
        CheckConstraint(
            "verdict IN ('bull','bear','neutral','unavailable')",
            name="ck_investment_stage_artifacts_verdict",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 100",
            name="ck_investment_stage_artifacts_confidence_range",
        ),
        CheckConstraint(
            "stage_type IN ("
            "'market','news','portfolio_journal','watch_context','candidate_universe',"
            "'bull_reducer','bear_reducer','risk_review')",
            name="ck_investment_stage_artifacts_stage_type_v1",
        ),
        UniqueConstraint(
            "run_uuid", "stage_type", name="ix_investment_stage_artifacts_run_stage"
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    artifact_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        unique=True,
        server_default=text("gen_random_uuid()"),
    )
    run_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("review.investment_stage_runs.run_uuid", ondelete="CASCADE"),
        nullable=False,
    )
    stage_type: Mapped[str] = mapped_column(Text, nullable=False)
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_points: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    buy_evidence: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    sell_evidence: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    risk_evidence: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    missing_data: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    cited_snapshot_uuids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)),
        nullable=False,
        server_default=text("ARRAY[]::uuid[]"),
    )
    freshness_summary: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    model_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
