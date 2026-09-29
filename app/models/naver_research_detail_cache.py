"""Naver research detail cache (ROB-811, re-keyed for #930).

Immutable per-report cache. Rows written before the #930 endpoint migration
carry the legacy `company_read.naver?nid=X` report id; rows written after use
`api:{researchId}` keys from the m.stock.naver.com research detail JSON so the
two identifier namespaces can never collide. Stores only the two fields the
detail response yields (target price, rating). All writes go through
NaverResearchDetailCacheRepository.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import TIMESTAMP, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class NaverResearchDetailCache(Base):
    __tablename__ = "naver_research_detail_cache"

    nid: Mapped[str] = mapped_column(Text, primary_key=True)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    rating: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
