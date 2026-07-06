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
    Integer,
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
            "'candidate_pool','session_summary','briefing'"
            ")",
            name="kind",
        ),
        CheckConstraint(
            "created_by IN ('claude','operator','system','codex')",
            name="created_by",
        ),
        CheckConstraint(
            "readiness_label IS NULL OR readiness_label IN ("
            "'screen_grade','not_decision_ready',"
            "'ready_for_order_review','blocked'"
            ")",
            name="readiness_label",
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
        UniqueConstraint(
            "correlation_id",
            name="uq_analysis_artifacts_correlation_id",
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
    correlation_id: Mapped[str | None] = mapped_column(Text)
    account_scope: Mapped[str | None] = mapped_column(Text)
    # ROB-648 lifecycle fields. content_hash is server-computed over the
    # canonical payload JSON (nullable + lazy backfill on next save). version is
    # bumped in place on a correlation_id re-save whose payload content changed.
    # readiness_label is a caller-declared advisory (reduced enum, see CHECK).
    content_hash: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    readiness_label: Mapped[str | None] = mapped_column(Text)
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

    @property
    def is_stale(self) -> bool:
        if self.valid_until is None:
            return False
        from app.core.timezone import now_kst

        return self.valid_until < now_kst()

    @property
    def payload_size_bytes(self) -> int:
        import json

        # ensure_ascii=False so Korean text measures at its real UTF-8 size
        # instead of the ~6x escaped size (ROB-628 lesson).
        return len(
            json.dumps(self.payload, ensure_ascii=False, default=str).encode("utf-8")
        )
