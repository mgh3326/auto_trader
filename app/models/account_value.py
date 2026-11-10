"""Immutable account valuation observations and operator cash flows."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Date, Numeric, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class AccountValueSnapshot(Base):
    __tablename__ = "account_value_snapshots"
    __table_args__ = (
        UniqueConstraint("account", "market", "day"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    holdings: Mapped[list] = mapped_column(JSONB, nullable=False)
    cash: Mapped[dict] = mapped_column(JSONB, nullable=False)
    reasons: Mapped[list] = mapped_column(JSONB, nullable=False)
    fx: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )


class AccountExternalFlow(Base):
    __tablename__ = "account_external_flows"
    __table_args__ = {"schema": "review"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
