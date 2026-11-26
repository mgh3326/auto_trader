"""Append-only market-wide KR daily observations from Naver."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import TIMESTAMP, Boolean, Date, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class KRMarketBreadth(Base):
    __tablename__ = "kr_market_breadth"

    market: Mapped[str] = mapped_column(String(10), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    session_label: Mapped[str] = mapped_column(String(20), primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    up_count: Mapped[int] = mapped_column(Integer, nullable=False)
    down_count: Mapped[int] = mapped_column(Integer, nullable=False)
    flat_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_count: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_count: Mapped[int] = mapped_column(Integer, nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    first_received_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )


class KRMarketInvestorNet(Base):
    __tablename__ = "kr_market_investor_net"

    market: Mapped[str] = mapped_column(String(10), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    investor_code: Mapped[str] = mapped_column(String(20), primary_key=True)
    investor_name: Mapped[str | None] = mapped_column(String(40), nullable=True)
    net_amount: Mapped[Decimal] = mapped_column(Numeric(), nullable=False)
    buy_amount: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)
    sell_amount: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)
    buy_quantity: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)
    sell_quantity: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)
    amount_unit: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity_unit: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    first_received_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )


class KRMarketDepositCredit(Base):
    __tablename__ = "kr_market_deposit_credit"

    bizdate: Mapped[date] = mapped_column(Date, primary_key=True)
    customer_deposit: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)
    credit_loan: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)
    credit_loan_diff: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)
    beneficiary_certificate: Mapped[Decimal | None] = mapped_column(
        Numeric(), nullable=True
    )
    amount_unit: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    first_received_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
