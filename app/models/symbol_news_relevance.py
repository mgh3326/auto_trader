"""Symbol↔news relevance lifecycle (ROB-491).

One row owns the full lifecycle of "this article appeared in this symbol's
feed": provenance (feed_source/first_seen_at), pending state, and the external
judgment written back via the token-authed ingest route. auto_trader code never
sets ``excluded`` on its own — only the ingest path transitions status.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SymbolNewsRelevance(Base):
    __tablename__ = "symbol_news_relevance"

    __table_args__ = (
        UniqueConstraint(
            "article_id",
            "market",
            "symbol",
            name="uq_symbol_news_relevance_article_market_symbol",
        ),
        CheckConstraint(
            "market IN ('kr', 'us', 'crypto')",
            name="ck_symbol_news_relevance_market",
        ),
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'excluded')",
            name="ck_symbol_news_relevance_status",
        ),
        CheckConstraint(
            "relationship IS NULL OR relationship IN "
            "('direct', 'material_indirect', 'incidental', 'unrelated')",
            name="ck_symbol_news_relevance_relationship",
        ),
        CheckConstraint(
            "relevance IS NULL OR relevance IN ('high', 'medium', 'low')",
            name="ck_symbol_news_relevance_relevance",
        ),
        CheckConstraint(
            "price_relevance IS NULL OR price_relevance IN "
            "('catalyst', 'explainer', 'background', 'none')",
            name="ck_symbol_news_relevance_price_relevance",
        ),
        Index(
            "ix_symbol_news_relevance_market_symbol_status",
            "market",
            "symbol",
            "status",
        ),
        Index(
            "ix_symbol_news_relevance_status_first_seen",
            "status",
            "first_seen_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    article_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("news_articles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    market: Mapped[str] = mapped_column(String(20), nullable=False)
    symbol: Mapped[str] = mapped_column(String(40), nullable=False)
    feed_source: Mapped[str] = mapped_column(String(40), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    relationship: Mapped[str | None] = mapped_column(String(20), nullable=True)
    relevance: Mapped[str | None] = mapped_column(String(10), nullable=True)
    price_relevance: Mapped[str | None] = mapped_column(String(20), nullable=True)
    score: Mapped[float | None] = mapped_column(nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    judged_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    judged_at: Mapped[datetime | None] = mapped_column(nullable=True)
    hints: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    def __repr__(self) -> str:
        return (
            "<SymbolNewsRelevance("
            f"article_id={self.article_id}, market='{self.market}', "
            f"symbol='{self.symbol}', status='{self.status}')>"
        )
