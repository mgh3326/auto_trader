"""Schemas for the n8n news endpoint."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "N8nNewsItem",
    "N8nNewsSummary",
    "N8nNewsResponse",
]


class N8nNewsItem(BaseModel):
    id: int = Field(..., description="Article DB id")
    title: str = Field(..., description="Article title")
    url: str = Field(..., description="Article URL")
    source: str | None = Field(None, description="News source (매일경제, 연합뉴스 등)")
    feed_source: str | None = Field(None, description="RSS feed source key")
    summary: str | None = Field(None, description="LLM-generated summary")
    content_preview: str | None = Field(
        None, description="First 300 chars of article body"
    )
    published_at: str | None = Field(None, description="Publication time ISO8601 KST")
    keywords: list[str] | None = Field(None, description="Extracted keywords")
    stock_symbol: str | None = Field(None, description="Related stock symbol")
    stock_name: str | None = Field(None, description="Related stock name")

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={
            "example": {
                "id": 1,
                "title": "삼성전자, 1분기 실적 예상치 상회",
                "url": "https://news.example.com/article1",
                "source": "매일경제",
                "feed_source": "mk_stock",
                "summary": "삼성전자가 1분기 매출 80조원을 기록하며...",
                "content_preview": "삼성전자가 1분기 매출 80조원을 기록하며...",
                "published_at": "2026-03-29T08:30:00+09:00",
                "keywords": ["삼성전자", "실적"],
                "stock_symbol": "005930",
                "stock_name": "삼성전자",
            }
        }
    )


class N8nNewsSummary(BaseModel):
    total: int = Field(..., description="Total articles returned")
    sources: list[str] = Field(default_factory=list, description="Unique source names")
    date_range: str = Field("", description="e.g. 2026-03-30 07:00 ~ 09:00")


class N8nNewsResponse(BaseModel):
    success: bool = Field(True, description="Whether the request succeeded")
    as_of: str = Field(..., description="Response timestamp KST ISO8601")
    summary: N8nNewsSummary = Field(..., description="Aggregated summary")
    items: list[N8nNewsItem] = Field(default_factory=list, description="News articles")
    discord_title: str = Field("", description="Pre-formatted Discord message title")
    discord_body: str = Field("", description="Pre-formatted Discord thread body")
    errors: list[dict[str, object]] = Field(
        default_factory=list, description="Non-fatal errors"
    )

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={
            "example": {
                "success": True,
                "as_of": "2026-03-29T08:30:00+09:00",
                "summary": {
                    "total": 5,
                    "sources": ["매일경제", "연합뉴스"],
                    "date_range": "2026-03-29 07:00 ~ 09:00",
                },
                "items": [],
                "discord_title": "📰 장전 뉴스 브리핑 (2026-03-29 토) — 5건",
                "discord_body": "",
                "errors": [],
            }
        }
    )
