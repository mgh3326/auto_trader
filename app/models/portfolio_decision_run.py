"""Persisted Decision Desk run snapshots."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PortfolioDecisionRun(Base):
    """Immutable JSONB snapshot for a generated Decision Desk slate."""

    __tablename__ = "portfolio_decision_runs"

    __table_args__ = (
        Index(
            "ix_portfolio_decision_runs_user_generated_at",
            "user_id",
            "generated_at",
        ),
        Index("ix_portfolio_decision_runs_market_scope", "market_scope"),
    )

    run_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        onupdate=_utcnow,
    )
    market_scope: Mapped[str] = mapped_column(String(20), nullable=False)
    mode: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    facets: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    symbol_groups: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    warnings: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    user = relationship("User", backref="portfolio_decision_runs")

    def __repr__(self) -> str:
        return (
            "<PortfolioDecisionRun("
            f"run_id='{self.run_id}', user_id={self.user_id}, "
            f"market_scope='{self.market_scope}')>"
        )
