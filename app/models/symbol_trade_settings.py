"""
Symbol Trade Settings Model

종목별 분할 매수 수량 설정을 저장하는 모델
"""
from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    Enum,
    Numeric,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.trading import InstrumentType


class SymbolTradeSettings(Base):
    """종목별 거래 설정 테이블"""

    __tablename__ = "symbol_trade_settings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol: Mapped[str] = mapped_column(Text, unique=True, nullable=False, index=True)
    instrument_type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, name="instrument_type", create_type=False),
        nullable=False,
    )

    # 분할 매수 설정 - 주문당 수량 (주식: 주, 코인: 개)
    # Numeric(18, 8)로 소수점 8자리까지 지원 (코인 소수점 거래용)
    buy_quantity_per_order: Mapped[float] = mapped_column(
        Numeric(18, 8), nullable=False
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

    def __repr__(self) -> str:
        return (
            f"<SymbolTradeSettings(symbol={self.symbol}, "
            f"type={self.instrument_type}, qty={self.buy_quantity_per_order})>"
        )
