"""DCA (Dollar Cost Averaging) Plan Models

분할 매수(DCA) 계획 및 단계를 저장하는 모델
"""

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class DcaPlanStatus(str, enum.Enum):
    """DCA 플랜 상태"""

    ACTIVE = "active"  # 진행 중
    COMPLETED = "completed"  # 완료
    CANCELLED = "cancelled"  # 취소
    EXPIRED = "expired"  # 만료


class DcaStepStatus(str, enum.Enum):
    """DCA 단계 상태"""

    PENDING = "pending"  # 대기
    ORDERED = "ordered"  # 주문 접수
    PARTIAL = "partial"  # 부분 체결
    FILLED = "filled"  # 전체 체결
    CANCELLED = "cancelled"  # 취소
    SKIPPED = "skipped"  # 건너뜀


class DcaPlan(Base):
    """DCA 분할 매수 플랜 테이블"""

    __tablename__ = "dca_plans"
    __table_args__ = (
        Index("ix_dca_plans_user_status", "user_id", "status"),
        Index("ix_dca_plans_symbol", "symbol"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    market: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # equity_kr, equity_us, crypto

    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False
    )  # 총 투자 금액
    splits: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 분할 횟수
    strategy: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # support, equal, aggressive

    status: Mapped[DcaPlanStatus] = mapped_column(
        Enum(DcaPlanStatus, name="dca_plan_status", create_type=False),
        nullable=False,
        default=DcaPlanStatus.ACTIVE,
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
    completed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )  # 플랜 완료 시간

    # RSI 관련 (계산 시점 저장용)
    rsi_14: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)

    # Relationships
    steps: Mapped[list["DcaPlanStep"]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="DcaPlanStep.step_number",
    )

    def __repr__(self) -> str:
        return (
            f"<DcaPlan(id={self.id}, user_id={self.user_id}, "
            f"symbol={self.symbol}, status={self.status.value})>"
        )


class DcaPlanStep(Base):
    """DCA 분할 매수 단계 테이블"""

    __tablename__ = "dca_plan_steps"
    __table_args__ = (
        Index("ix_dca_plan_steps_plan_id", "plan_id"),
        Index("ix_dca_plan_steps_order_id", "order_id"),
        UniqueConstraint("plan_id", "step_number", name="uq_dca_plan_step"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("dca_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_number: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 1-indexed

    # 목표값
    target_price: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False
    )  # 목표 가격
    target_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False
    )  # 목표 금액
    target_quantity: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False
    )  # 목표 수량

    # 상태
    status: Mapped[DcaStepStatus] = mapped_column(
        Enum(DcaStepStatus, name="dca_step_status", create_type=False),
        nullable=False,
        default=DcaStepStatus.PENDING,
    )

    # 체결값
    filled_price: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8), nullable=True
    )  # 체결 가격
    filled_quantity: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8), nullable=True
    )  # 체결 수량
    filled_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 2), nullable=True
    )  # 체결 금액

    # 주문 정보
    order_id: Mapped[str | None] = mapped_column(
        Text, nullable=True, index=True
    )  # 브로커 주문 ID

    # 시간
    ordered_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )  # 주문 시간
    filled_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )  # 체결 시간

    # 추가 메타데이터
    level_source: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # support, equal_spaced, interpolated, etc.

    # Relationships
    plan: Mapped["DcaPlan"] = relationship(back_populates="steps")

    def __repr__(self) -> str:
        return (
            f"<DcaPlanStep(id={self.id}, plan_id={self.plan_id}, "
            f"step={self.step_number}, status={self.status.value})>"
        )
