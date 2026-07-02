"""Analysis artifact persistence (ROB-637)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class AnalysisArtifact(Base):
    """Persisted analysis artifacts for cross-session analysis reuse."""

    __tablename__ = "analysis_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "artifact_uuid",
            name="uq_analysis_artifacts_artifact_uuid",
        ),
        CheckConstraint(
            "market IN ('kr','us','crypto')",
            name="market",
        ),
        CheckConstraint(
            "kind IN ("
            "'screening_ranking','profit_taking_verdicts',"
            "'support_resistance_map','flow_assessment',"
            "'candidate_pool','session_summary'"
            ")",
            name="kind",
        ),
        CheckConstraint(
            "created_by IN ('claude','operator','system')",
            name="created_by",
        ),
        Index(
            "ix_analysis_artifacts_kind_market_as_of",
            "kind",
            "market",
            text("as_of DESC"),
        ),
        Index(
            "ix_analysis_artifacts_symbols_gin",
            "symbols",
            postgresql_using="gin",
        ),
        Index(
            "ix_analysis_artifacts_payload_gin",
            "payload",
            postgresql_using="gin",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    artifact_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        default=uuid.uuid4,
    )
    market: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    symbols: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        default=list,
        server_default=text("'{}'"),
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    as_of: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
    )
    valid_until: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
    session_label: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="claude",
        server_default=text("'claude'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
