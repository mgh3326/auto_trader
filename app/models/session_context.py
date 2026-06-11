"""Operator session context persistence (ROB-516)."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Date,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func, text

from app.models.base import Base


class OperatorSessionContext(Base):
    """Append-only operator handoff entries for trading-session continuity."""

    __tablename__ = "operator_session_context"
    __table_args__ = (
        UniqueConstraint(
            "entry_uuid",
            name="uq_operator_session_context_entry_uuid",
        ),
        CheckConstraint(
            "market IN ('kr','us','crypto')",
            name="market",
        ),
        CheckConstraint(
            "account_scope IS NULL OR account_scope IN "
            "('kis_live','kis_mock','alpaca_paper','upbit_live')",
            name="account_scope",
        ),
        CheckConstraint(
            "entry_type IN ("
            "'plan','decision','deferred','rejected_candidate','constraint',"
            "'open_question','next_action','handoff_note'"
            ")",
            name="entry_type",
        ),
        CheckConstraint(
            "created_by IN ('claude','operator','system')",
            name="created_by",
        ),
        CheckConstraint(
            "jsonb_typeof(refs) = 'object'",
            name="refs_object",
        ),
        Index(
            "ix_operator_session_context_market_date_created",
            "market",
            "kst_date",
            "created_at",
        ),
        Index(
            "ix_operator_session_context_entry_type_date",
            "entry_type",
            "kst_date",
        ),
        Index(
            "ix_operator_session_context_refs_gin",
            "refs",
            postgresql_using="gin",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entry_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        default=uuid.uuid4,
    )
    kst_date: Mapped[date] = mapped_column(Date, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    account_scope: Mapped[str | None] = mapped_column(Text)
    entry_type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    refs: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    created_by: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="claude",
        server_default=text("'claude'"),
    )
    session_label: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
