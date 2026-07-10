"""Naver research detail-page cache (ROB-811).

Immutable per-report cache for `company_read.naver?nid=X` detail pages. Stores
only the two fields the detail page yields (target price, rating). All writes go
through NaverResearchDetailCacheRepository.
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