"""
Symbol Trade Settings Model

종목별 분할 매수 수량 설정을 저장하는 모델
사용자별 기본 거래 설정도 포함
"""
from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    Enum,
    ForeignKey,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.trading import InstrumentType


class UserTradeDefaults(Base):
    """사용자별 기본 거래 설정 테이블

    코인, 국내주식, 해외주식 각각의 기본 매수 금액/수량을 설정
    """

    __tablename__ = "user_trade_defaults"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )

    # 코인 기본 매수 금액 (KRW) - 설정 없는 코인에 적용
    crypto_default_buy_amount: Mapped[float] = mapped_column(
        Numeric(18, 2), nullable=False, default=10000
    )
    # 코인 최소 주문 금액 (KRW)
    crypto_min_order_amount: Mapped[float] = mapped_column(
        Numeric(18, 2), nullable=False, default=5000
    )

    # 국내주식 기본 매수 수량 (주) - None이면 설정 없는 종목 매수 안함
    equity_kr_default_buy_quantity: Mapped[float | None] = mapped_column(
        Numeric(18, 2), nullable=True, default=None
    )

    # 해외주식 기본 매수 수량 (주) - None이면 설정 없는 종목 매수 안함
    equity_us_default_buy_quantity: Mapped[float | None] = mapped_column(
        Numeric(18, 2), nullable=True, default=None
    )

    # 해외주식 기본 매수 금액 (USD) - None이면 설정 없는 종목 매수 안함
    equity_us_default_buy_amount: Mapped[float | None] = mapped_column(
        Numeric(18, 2), nullable=True, default=None
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    user = relationship("User", backref="trade_defaults")

    def __repr__(self) -> str:
        return (
            f"<UserTradeDefaults(user_id={self.user_id}, "
            f"crypto={self.crypto_default_buy_amount})>"
        )


class SymbolTradeSettings(Base):
    """종목별 거래 설정 테이블

    사용자별로 각 종목에 대한 매수 수량/금액을 설정
    """

    __tablename__ = "symbol_trade_settings"
    __table_args__ = (
        UniqueConstraint("user_id", "symbol", name="uq_user_symbol"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    instrument_type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, name="instrument_type", create_type=False),
        nullable=False,
    )

    # 분할 매수 설정 - 주문당 수량/금액
    # 주식: 주 단위, 코인: KRW 금액 단위
    # Numeric(18, 8)로 소수점 8자리까지 지원 (코인 소수점 거래용)
    buy_quantity_per_order: Mapped[float] = mapped_column(
        Numeric(18, 8), nullable=False
    )

    # 주문할 가격대 수 (1~4) - 낮은 가격부터 순서대로
    # 1: appropriate_buy_min만
    # 2: appropriate_buy_min, appropriate_buy_max
    # 3: appropriate_buy_min, appropriate_buy_max, buy_hope_min
    # 4: 전체 4개 가격대 (기본값)
    buy_price_levels: Mapped[int] = mapped_column(
        default=4, nullable=False
    )

    # 선택적 필드
    exchange_code: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # 해외주식 거래소 코드 (NASD, NYSE 등)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    user = relationship("User", backref="symbol_trade_settings")

    def __repr__(self) -> str:
        return (
            f"<SymbolTradeSettings(user_id={self.user_id}, symbol={self.symbol}, "
            f"type={self.instrument_type}, qty={self.buy_quantity_per_order})>"
        )
