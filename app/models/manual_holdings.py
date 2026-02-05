"""
Manual Holdings Models

외부 브로커(토스 등) 수동 잔고 등록을 위한 모델
"""

import enum
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


class BrokerType(str, enum.Enum):
    """브로커 타입"""

    kis = "kis"  # 한국투자증권
    toss = "toss"  # 토스증권
    upbit = "upbit"  # 업비트 (암호화폐)


class MarketType(str, enum.Enum):
    """시장 타입"""

    KR = "KR"  # 국내주식
    US = "US"  # 해외주식
    CRYPTO = "CRYPTO"  # 암호화폐


class BrokerAccount(Base):
    """브로커 계좌 테이블

    사용자별 브로커 계좌를 관리
    """

    __tablename__ = "broker_accounts"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "broker_type", "account_name", name="uq_broker_account"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    broker_type: Mapped[BrokerType] = mapped_column(
        Enum(BrokerType, name="broker_type"), nullable=False
    )
    account_name: Mapped[str] = mapped_column(Text, nullable=False, default="기본 계좌")
    is_mock: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
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

    # Relationships
    user = relationship("User", backref="broker_accounts")
    holdings: Mapped[list["ManualHolding"]] = relationship(
        back_populates="broker_account", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<BrokerAccount(id={self.id}, user_id={self.user_id}, "
            f"broker={self.broker_type}, name={self.account_name})>"
        )


class StockAlias(Base):
    """종목 별칭 테이블

    토스 등에서 사용하는 종목 별칭을 정규 티커에 매핑
    예: "버크셔 해서웨이 B" -> "BRK.B"
    """

    __tablename__ = "stock_aliases"
    __table_args__ = (UniqueConstraint("alias", "market_type", name="uq_alias_market"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticker: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    market_type: Mapped[MarketType] = mapped_column(
        Enum(MarketType, name="market_type"), nullable=False
    )
    alias: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    source: Mapped[str] = mapped_column(
        Text, nullable=False, default="user"
    )  # toss, user, kis

    def __repr__(self) -> str:
        return (
            f"<StockAlias(ticker={self.ticker}, alias={self.alias}, "
            f"market={self.market_type})>"
        )


class ManualHolding(Base):
    """수동 등록 보유 종목 테이블

    외부 브로커의 보유 종목을 수동으로 등록
    """

    __tablename__ = "manual_holdings"
    __table_args__ = (
        UniqueConstraint(
            "broker_account_id", "ticker", "market_type", name="uq_holding_ticker"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    broker_account_id: Mapped[int] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ticker: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    market_type: Mapped[MarketType] = mapped_column(
        Enum(MarketType, name="market_type", create_type=False), nullable=False
    )
    quantity: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    avg_price: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    display_name: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # 사용자 정의 표시명
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    broker_account: Mapped[BrokerAccount] = relationship(back_populates="holdings")

    def __repr__(self) -> str:
        return (
            f"<ManualHolding(id={self.id}, ticker={self.ticker}, "
            f"qty={self.quantity}, avg_price={self.avg_price})>"
        )
