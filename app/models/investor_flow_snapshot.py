from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class InvestorFlowSnapshot(Base):
    """Durable KR investor-flow/ranking snapshot for read-only /invest cards."""

    __tablename__ = "investor_flow_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "market",
            "symbol",
            "snapshot_date",
            "source",
            name="uq_investor_flow_snapshots_market_symbol_date_source",
        ),
        CheckConstraint("market IN ('kr')", name="ck_investor_flow_snapshots_market"),
        CheckConstraint(
            "source IN ('naver_finance', 'kis', 'manual')",
            name="ck_investor_flow_snapshots_source",
        ),
        Index(
            "ix_investor_flow_snapshots_market_symbol_date",
            "market",
            "symbol",
            "snapshot_date",
        ),
        Index(
            "ix_investor_flow_snapshots_market_foreign_rank",
            "market",
            "foreign_net_buy_rank",
            postgresql_where=text("foreign_net_buy_rank IS NOT NULL"),
        ),
        Index(
            "ix_investor_flow_snapshots_market_institution_rank",
            "market",
            "institution_net_buy_rank",
            postgresql_where=text("institution_net_buy_rank IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    market: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    close: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    change_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    foreign_holding_shares: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    foreign_holding_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 4), nullable=True
    )

    foreign_net: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    institution_net: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    individual_net: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    foreign_net_buy_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    foreign_net_sell_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    institution_net_buy_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    institution_net_sell_rank: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )

    double_buy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    double_sell: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    foreign_consecutive_buy_days: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    foreign_consecutive_sell_days: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    institution_consecutive_buy_days: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    institution_consecutive_sell_days: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    individual_consecutive_buy_days: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    individual_consecutive_sell_days: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )

    source: Mapped[str] = mapped_column(String(32), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
