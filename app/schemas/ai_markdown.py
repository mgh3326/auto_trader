"""AI Markdown Export Schemas"""
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class PresetType(StrEnum):
    """AI Markdown 프리셋 타입"""
    PORTFOLIO_STANCE = "portfolio_stance"
    STOCK_STANCE = "stock_stance"
    STOCK_ADD_OR_HOLD = "stock_add_or_hold"


class PortfolioMarkdownRequest(BaseModel):
    """포트폴리오 Markdown 생성 요청"""
    preset: PresetType = Field(
        default=PresetType.PORTFOLIO_STANCE,
        description="생성할 Markdown 프리셋"
    )
    include_market: str = Field(
        default="ALL",
        description="포함할 시장 (ALL, KR, US, CRYPTO)"
    )


class StockMarkdownRequest(BaseModel):
    """종목 상세 Markdown 생성 요청"""
    preset: PresetType = Field(
        ...,  # required
        description="생성할 Markdown 프리셋 (stock_stance 또는 stock_add_or_hold)"
    )
    symbol: str = Field(..., description="종목 코드")
    market_type: str = Field(..., description="시장 타입 (KR, US, CRYPTO)")


class MarkdownResponse(BaseModel):
    """Markdown 생성 응답"""
    success: bool = Field(default=True)
    preset: PresetType
    title: str = Field(description="생성된 Markdown 제목")
    content: str = Field(description="생성된 Markdown 내용")
    filename: str = Field(description="다운로드용 파일명")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="추가 메타데이터 (포지션 수, 생성 시간 등)"
    )
    error: str | None = Field(default=None, description="에러 메시지 (실패 시)")


class InvestmentProfile(BaseModel):
    """투자 성향 프로필 (공통 문구)"""
    style: str = Field(
        default="분할매수 선호, 단기 예측보다 조건 기반 대응 선호",
        description="투자 스타일"
    )
    stop_loss_philosophy: str = Field(
        default="과도한 손절보다 논리 훼손 여부를 중시",
        description="손절 철학"
    )
    leverage_preference: str = Field(
        default="레버리지 ETF는 축소 지향",
        description="레버리지 선호도"
    )
    sector_preference: str = Field(
        default="섹터 리더 선호",
        description="섹터 선호"
    )

    def to_markdown(self) -> str:
        """투자 성향을 Markdown 형식으로 변환"""
        return f"""- {self.style}
- {self.stop_loss_philosophy}
- {self.leverage_preference}
- {self.sector_preference}"""
