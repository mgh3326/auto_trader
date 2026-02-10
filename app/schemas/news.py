"""Pydantic schemas for news analysis requests and responses."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class NewsArticleCreate(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048, description="뉴스 기사 URL")
    title: str = Field(..., min_length=1, max_length=500, description="뉴스 제목")
    source: str | None = Field(None, max_length=100, description="뉴스 출처")
    author: str | None = Field(None, max_length=200, description="기자명")
    content: str = Field(..., min_length=1, description="기사 본문")
    stock_symbol: str | None = Field(None, max_length=20, description="관련 종목 코드")
    stock_name: str | None = Field(None, max_length=100, description="관련 종목명")
    published_at: datetime | None = Field(None, description="기사 발행일시")

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        return v.strip()

    @field_validator("title")
    @classmethod
    def validate_title(cls, v: str) -> str:
        return v.strip()


class NewsArticleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    url: str
    title: str
    source: str | None
    author: str | None
    content: str
    summary: str | None
    stock_symbol: str | None
    stock_name: str | None
    published_at: datetime | None
    scraped_at: datetime
    user_id: int | None
    created_at: datetime
    updated_at: datetime | None


class NewsAnalysisResultResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    article_id: int
    model_name: str
    sentiment: str
    sentiment_score: float | None
    summary: str
    key_points: list[str]
    topics: list[str] | None
    price_impact: str | None
    price_impact_score: float | None
    confidence: int
    analysis_quality: str | None
    processing_time_ms: int | None
    created_at: datetime
    updated_at: datetime | None


class NewsAnalysisRequest(BaseModel):
    url: str = Field(..., description="분석할 뉴스 기사 URL")
    content: str = Field(..., description="기사 본문 (스크래핑된 텍스트)")
    title: str = Field(..., description="뉴스 제목")
    source: str | None = Field(None, description="뉴스 출처")
    stock_symbol: str | None = Field(None, description="관련 종목 코드 (선택사항)")
    stock_name: str | None = Field(None, description="관련 종목명 (선택사항)")

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        return v.strip()

    @field_validator("content")
    @classmethod
    def validate_content(cls, v: str) -> str:
        return v.strip()


class NewsAnalysisResponse(BaseModel):
    article: NewsArticleResponse
    analysis: NewsAnalysisResultResponse


class NewsQueryParams(BaseModel):
    stock_symbol: str | None = Field(None, description="종목 코드로 필터링")
    sentiment: str | None = Field(
        None, description="감정으로 필터링 (positive/negative/neutral)"
    )
    source: str | None = Field(None, description="뉴스 출처로 필터링")
    limit: int = Field(10, ge=1, le=100, description="반환할 뉴스 수")
    offset: int = Field(0, ge=0, description="건너뛸 뉴스 수")


class NewsListResponse(BaseModel):
    total: int = Field(..., description="전체 뉴스 수")
    items: list[NewsArticleResponse] = Field(..., description="뉴스 리스트")
    page_info: dict[str, Any] = Field(..., description="페이지 정보")
