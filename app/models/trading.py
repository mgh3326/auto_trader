import enum

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    Enum,
    ForeignKey,
    Interval,
    Numeric,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class InstrumentType(str, enum.Enum):
    equity_kr = "equity_kr"  # 국내주식
    equity_us = "equity_us"  # 해외주식
    crypto = "crypto"  # 암호화폐
    forex = "forex"  # 환율
    index = "index"  # 지수


class NotifyChannel(str, enum.Enum):
    telegram = "telegram"
    email = "email"
    webhook = "webhook"


class Exchange(Base):
    __tablename__ = "exchanges"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    country: Mapped[str | None] = mapped_column(Text)
    tz: Mapped[str] = mapped_column(Text, nullable=False, default="Asia/Seoul")

    instruments: Mapped[list["Instrument"]] = relationship(back_populates="exchange")


class Instrument(Base):
    __tablename__ = "instruments"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    exchange_id: Mapped[int | None] = mapped_column(ForeignKey("exchanges.id"))
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    full_symbol: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, name="instrument_type"), nullable=False
    )
    base_currency: Mapped[str] = mapped_column(Text, nullable=False)
    tick_size: Mapped[float] = mapped_column(Numeric(18, 8), default=0.01)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    exchange: Mapped["Exchange"] = relationship(back_populates="instruments")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str | None] = mapped_column(Text, unique=True)
    username: Mapped[str | None] = mapped_column(Text, unique=True)
    hashed_password: Mapped[str | None] = mapped_column(Text)
    nickname: Mapped[str | None] = mapped_column(Text)
    tz: Mapped[str] = mapped_column(Text, default="Asia/Seoul", nullable=False)
    base_currency: Mapped[str] = mapped_column(Text, default="KRW", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[str] = mapped_column(
        TIMESTAMP(timezone=True), server_default="now()"
    )


class UserChannel(Base):
    __tablename__ = "user_channels"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[NotifyChannel] = mapped_column(
        Enum(NotifyChannel, name="notify_channel"), nullable=False
    )
    handle: Mapped[str] = mapped_column(Text, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class UserWatchItem(Base):
    __tablename__ = "user_watch_items"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text)
    desired_buy_px: Mapped[float | None] = mapped_column(Numeric(18, 8))
    target_sell_px: Mapped[float | None] = mapped_column(Numeric(18, 8))
    stop_loss_px: Mapped[float | None] = mapped_column(Numeric(18, 8))
    quantity: Mapped[float | None] = mapped_column(Numeric(18, 6))
    use_trailing: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    trailing_gap_pct: Mapped[float | None] = mapped_column(Numeric(9, 4))
    notify_cooldown: Mapped[str] = mapped_column(
        Interval(), default="1 hour", nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[str] = mapped_column(
        TIMESTAMP(timezone=True), server_default="now()"
    )
    updated_at: Mapped[str] = mapped_column(
        TIMESTAMP(timezone=True), server_default="now()", onupdate=None
    )
