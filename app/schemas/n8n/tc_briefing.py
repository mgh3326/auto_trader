from __future__ import annotations

from pydantic import BaseModel, Field


class N8nTcBriefingItem(BaseModel):
    """Single briefing item within a category."""

    symbol: str = Field(..., description="종목 심볼 (e.g. BTC, 005930)")
    name: str = Field(..., description="종목명 (e.g. 비트코인, SK하이닉스)")
    action: str = Field(..., description="액션 (e.g. 매도, 매수, 홀드, 추가매수)")
    reason_summary: str = Field(..., description="근거 요약 (1-2문장)")


class N8nTcBriefingCategory(BaseModel):
    """Category grouping for briefing items."""

    category: str = Field(..., description="카테고리 (매도/매수/홀드/추가매수)")
    items: list[N8nTcBriefingItem] = Field(default_factory=list)


class N8nTcBriefingRequest(BaseModel):
    """Request body for POST /api/n8n/tc-briefing."""

    issue_identifier: str = Field(
        ..., description="Paperclip issue identifier (e.g. ROB-94)"
    )
    title: str = Field(..., description="브리핑 제목")
    briefing_items: list[N8nTcBriefingCategory] = Field(
        ..., description="카테고리별 브리핑 항목"
    )
    paperclip_issue_url: str | None = Field(
        None, description="Paperclip issue deep link URL"
    )


class N8nTcBriefingResponse(BaseModel):
    """Response for POST /api/n8n/tc-briefing."""

    success: bool
    message_id: str | None = Field(None, description="Discord message ID if sent")
    errors: list[dict] = Field(default_factory=list)
