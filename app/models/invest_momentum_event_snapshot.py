from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class InvestMomentumEventSnapshot(Base):
    __tablename__ = "invest_momentum_event_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "surface",
            "snapshot_at",
            "trade_type",
            "market_type",
            "order_type",
            "symbol",
            name="uq_invest_momentum_event_snapshots_surface_params_symbol_at",
        ),
        CheckConstraint(
            "market = 'kr'", name="ck_invest_momentum_event_snapshots_market"
        ),
        CheckConstraint(
            "source = 'naver_stock'", name="ck_invest_momentum_event_snapshots_source"
        ),
        Index(
            "ix_invest_momentum_event_snapshots_date_order_rank",
            "trading_date",
            "order_type",
            "rank",
        ),
        Index(
            "ix_invest_momentum_event_snapshots_symbol_date", "symbol", "trading_date"
        ),
        Index(
            "ix_invest_momentum_event_snapshots_surface_params_at",
            "surface",
            "trade_type",
            "market_type",
            "order_type",
            "snapshot_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="naver_stock"
    )
    surface: Mapped[str] = mapped_column(String(80), nullable=False)
    market: Mapped[str] = mapped_column(String(8), nullable=False, default="kr")
    trade_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    market_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    order_type: Mapped[str] = mapped_column(String(32), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    change_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    change_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    trade_value: Mapped[Decimal | None] = mapped_column(Numeric(30, 2), nullable=True)
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric(30, 2), nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
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


class InvestThemeEventSnapshot(Base):
    __tablename__ = "invest_theme_event_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_at",
            "source_event_key",
            name="uq_invest_theme_event_snapshots_at_key",
        ),
        CheckConstraint("market = 'kr'", name="ck_invest_theme_event_snapshots_market"),
        CheckConstraint(
            "source = 'naver_stock'", name="ck_invest_theme_event_snapshots_source"
        ),
        CheckConstraint(
            "event_kind IN ('theme', 'upjong')",
            name="ck_invest_theme_event_snapshots_kind",
        ),
        Index(
            "ix_invest_theme_event_snapshots_date_kind_sort_rank",
            "trading_date",
            "event_kind",
            "sort_type",
            "rank",
        ),
        Index(
            "ix_invest_theme_event_snapshots_kind_key_at",
            "event_kind",
            "source_event_key",
            "snapshot_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="naver_stock"
    )
    surface: Mapped[str] = mapped_column(String(80), nullable=False)
    market: Mapped[str] = mapped_column(String(8), nullable=False, default="kr")
    event_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_event_key: Mapped[str] = mapped_column(String(160), nullable=False)
    naver_theme_no: Mapped[str | None] = mapped_column(String(40), nullable=True)
    naver_upjong_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    sort_type: Mapped[str] = mapped_column(String(32), nullable=False)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    market_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    change_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    trade_value: Mapped[Decimal | None] = mapped_column(Numeric(30, 2), nullable=True)
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric(30, 2), nullable=True)
    stock_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    leader_symbols: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
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

    stocks: Mapped[list[InvestThemeEventSnapshotStock]] = relationship(
        "InvestThemeEventSnapshotStock",
        back_populates="theme_snapshot",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class InvestThemeEventSnapshotStock(Base):
    __tablename__ = "invest_theme_event_snapshot_stocks"
    __table_args__ = (
        UniqueConstraint(
            "theme_snapshot_id",
            "order_type",
            "symbol",
            name="uq_invest_theme_event_snapshot_stocks_parent_order_symbol",
        ),
        Index("ix_invest_theme_event_snapshot_stocks_symbol", "symbol"),
        Index(
            "ix_invest_theme_event_snapshot_stocks_parent_rank",
            "theme_snapshot_id",
            "rank",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    theme_snapshot_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("invest_theme_event_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    order_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    change_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    change_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    trade_value: Mapped[Decimal | None] = mapped_column(Numeric(30, 2), nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    theme_snapshot: Mapped[InvestThemeEventSnapshot] = relationship(
        "InvestThemeEventSnapshot",
        back_populates="stocks",
    )
