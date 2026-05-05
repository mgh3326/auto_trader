"""Tests for app.services.research_news_service (ROB-115)."""
from __future__ import annotations
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock
import pytest
from app.services import research_news_service

class TestFetchSymbolNewsKR:
    @pytest.mark.asyncio
    async def test_returns_normalized_articles_for_kr_symbol(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_naver_payload = [
            {
                "title": "삼성전자 호실적 발표",
                "url": "https://finance.naver.com/item/news_read.naver?code=005930&id=1",
                "source": "한국경제",
                "datetime": "2026-05-05T09:30",
            },
            {
                "title": "반도체 업황 회복",
                "url": "https://finance.naver.com/item/news_read.naver?code=005930&id=2",
                "source": "매일경제",
                "datetime": "2026-05-04",
            },
        ]
        monkeypatch.setattr(
            research_news_service,
            "_naver_fetch_news",
            AsyncMock(return_value=fake_naver_payload),
        )
        result = await research_news_service.fetch_symbol_news(
            "005930", "equity_kr", limit=20
        )
        assert len(result) == 2
        first = result[0]
        assert first.title == "삼성전자 호실적 발표"
        assert first.url.startswith("https://finance.naver.com/")
        assert first.source == "한국경제"
        assert first.provider == "naver"
        assert isinstance(first.published_at, datetime)
        assert first.summary is None
