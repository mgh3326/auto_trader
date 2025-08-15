from sqlalchemy import (
    BigInteger, Text, Boolean, Numeric, Enum, ForeignKey, TIMESTAMP, Interval, text
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.base import Base
import enum

from app.models.trading import InstrumentType


class PromptResult(Base):
    __tablename__ = "prompt_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)  # 종목 코드
    name: Mapped[str] = mapped_column(Text, nullable=False)  # 종목명
    model_name: Mapped[str] = mapped_column(Text, nullable=True)  # 종목명
    instrument_type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, name="instrument_type"), nullable=False
    )
    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    updated_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"), onupdate=None)
