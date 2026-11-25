"""hk 6503 — per-symbol, per-category KR trading flag state.

One row per (symbol, category). Absence of a row means *unknown*, never
``none``. Writes go only through ``app/services/kr_trading_flags/store.py``.
The write guard keeps the newest observation (``observed_at``), and a
``unmapped`` observation never overwrites a mapped state.
"""

from datetime import date, datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, Date, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class KRSymbolTradingFlag(Base):
    __tablename__ = "kr_symbol_trading_flags"
    __table_args__ = (
        CheckConstraint(
            "symbol ~ '^[0-9A-Z]{6}$'",
            name="ck_kr_symbol_trading_flags_symbol_format",
        ),
        CheckConstraint(
            "category IN ('management','market_alert','delisting_trade','trading_halt')",
            name="ck_kr_symbol_trading_flags_category",
        ),
        CheckConstraint(
            "source IN ('kis_master','naver_intraday')",
            name="ck_kr_symbol_trading_flags_source",
        ),
        CheckConstraint(
            "(category = 'management' AND state IN ('designated','none','unmapped'))"
            " OR (category = 'market_alert' AND"
            " state IN ('none','caution','warning','risk','unmapped'))"
            " OR (category = 'delisting_trade' AND"
            " state IN ('in_progress','none','unmapped'))"
            " OR (category = 'trading_halt' AND state IN ('halted','none','unmapped'))",
            name="ck_kr_symbol_trading_flags_state_by_category",
        ),
    )

    symbol: Mapped[str] = mapped_column(String(6), primary_key=True)
    category: Mapped[str] = mapped_column(String(20), primary_key=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    raw_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
