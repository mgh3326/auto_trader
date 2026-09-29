"""#925 — operator-imported KRX after-market eligibility list.

One row per symbol on the KRX-published after-market (16:00-20:00 KST)
eligibility list. The table is a full snapshot: an import replaces every
row in one transaction, so all rows share ``list_asof``/``list_source`` and
absence from a non-empty table means "not on the list". An empty table means
no list has been imported and every symbol reads not-eligible. Writes go
only through ``kr_symbol_universe_service.replace_krx_after_market_list``.
"""

from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class KrxAfterMarketEligibility(Base):
    __tablename__ = "krx_after_market_eligibility"
    __table_args__ = (
        CheckConstraint(
            "symbol ~ '^[0-9A-Z]{6}$'",
            name="ck_krx_after_market_eligibility_symbol_format",
        ),
        CheckConstraint(
            "length(btrim(list_source)) > 0",
            name="ck_krx_after_market_eligibility_list_source_nonblank",
        ),
    )

    symbol: Mapped[str] = mapped_column(String(6), primary_key=True)
    list_source: Mapped[str] = mapped_column(Text, nullable=False)
    list_asof: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    imported_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
