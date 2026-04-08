"""AI Advisor service — orchestrates context generation and provider dispatch."""

from __future__ import annotations

import logging

from app.core.config import settings
from app.schemas.ai_advisor import AiAdviceResponse
from app.schemas.ai_markdown import PresetType
from app.services.ai_markdown_service import AIMarkdownService
from app.services.ai_providers.base import AiProvider
from app.services.ai_providers.gemini_provider import GeminiProvider
from app.services.ai_providers.openai_provider import OpenAIProvider
from app.services.portfolio_overview_service import PortfolioOverviewService
from app.services.portfolio_position_detail_service import (
    PortfolioPositionDetailService,
)

logger = logging.getLogger(__name__)

ADVISOR_SYSTEM_TEMPLATE = """당신은 투자 분석 보조입니다. 아래 컨텍스트를 바탕으로 사용자의 질문에 답합니다.

## 답변 원칙
- 단정적 투자 권유 금지. 시나리오 기반으로 정리
- bullish / base / bearish 또는 hold / add / reduce 복수 스탠스 제시
- 각 스탠스를 택할 조건을 명시
- 사용자의 현재 보유 상태(평단, 비중, thesis, target/stop)를 고려
- 불확실성, 추가 확인 포인트, 깨진 가정이 있으면 명시
- 답변은 실행 가능한 체크리스트 형태 선호

## 답변 구조
1. 현재 해석 (2-3문장)
2. 가능한 스탠스 2-3개 + 각 조건
3. 당장 확인할 지표/뉴스/가격대
4. 한 줄 결론

## 주의
- 이 도구는 투자 자문이 아닌 분석 보조입니다
- 데이터 기준 시점 이후 변동이 있을 수 있습니다

---
{context}
"""


def extract_context_before_question(content: str) -> str:
    """Extract content before the '## 질문' section.

    Falls back to the full content if the marker is not found,
    so this stays safe even if AIMarkdownService output format changes.
    """
    marker = "\n## 질문"
    idx = content.find(marker)
    if idx != -1:
        return content[:idx].rstrip()
    return content


def get_configured_providers() -> dict[str, AiProvider]:
    """Build provider dict from current settings. No DB needed."""
    providers: dict[str, AiProvider] = {}
    if settings.openai_api_key:
        providers["openai"] = OpenAIProvider(api_key=settings.openai_api_key)
    if settings.gemini_advisor_api_key:
        providers["gemini"] = GeminiProvider(api_key=settings.gemini_advisor_api_key)
    if settings.grok_api_key:
        providers["grok"] = OpenAIProvider(
            api_key=settings.grok_api_key,
            base_url="https://api.x.ai/v1",
            provider_name="grok",
            default_model="grok-3-mini",
        )
    return providers


class AiAdvisorService:
    """Orchestrates markdown context generation and LLM provider dispatch."""

    def __init__(
        self,
        markdown_service: AIMarkdownService,
        overview_service: PortfolioOverviewService,
        detail_service: PortfolioPositionDetailService,
    ) -> None:
        self.markdown_service = markdown_service
        self.overview_service = overview_service
        self.detail_service = detail_service
        self.providers = get_configured_providers()

    def available_providers(self) -> list[dict[str, str]]:
        return [
            {"name": name, "default_model": p.default_model}
            for name, p in self.providers.items()
        ]

    async def ask(
        self,
        *,
        user_id: int,
        scope: str,
        preset: PresetType,
        provider: str,
        question: str,
        model: str | None = None,
        market_type: str | None = None,
        symbol: str | None = None,
        include_market: str = "ALL",
    ) -> AiAdviceResponse:
        # 1. Generate context via existing AIMarkdownService
        context = await self._generate_context(
            user_id=user_id,
            scope=scope,
            preset=preset,
            market_type=market_type,
            symbol=symbol,
            include_market=include_market,
        )

        # 2. Build system prompt
        system_prompt = ADVISOR_SYSTEM_TEMPLATE.format(context=context)

        # 3. Dispatch to provider
        result = await self.providers[provider].ask(
            system_prompt=system_prompt,
            user_message=question,
            model=model,
            timeout=settings.ai_advisor_timeout,
        )

        # 4. Build response
        return AiAdviceResponse(
            success=True,
            answer=result.answer,
            provider=result.provider,
            model=result.model,
            usage=result.usage,
            elapsed_ms=result.elapsed_ms,
        )

    async def _generate_context(
        self,
        *,
        user_id: int,
        scope: str,
        preset: PresetType,
        market_type: str | None,
        symbol: str | None,
        include_market: str,
    ) -> str:
        if scope == "portfolio":
            portfolio_data = await self.overview_service.get_overview(
                user_id=user_id,
                market=include_market,
            )
            result = self.markdown_service.generate_portfolio_stance_markdown(
                portfolio_data,
            )
        else:
            stock_data = await self.detail_service.get_page_payload(
                user_id=user_id,
                market_type=market_type,
                symbol=symbol,
            )
            if preset == PresetType.STOCK_ADD_OR_HOLD:
                result = self.markdown_service.generate_stock_add_or_hold_markdown(
                    stock_data,
                )
            else:
                result = self.markdown_service.generate_stock_stance_markdown(
                    stock_data,
                )

        return extract_context_before_question(result["content"])
