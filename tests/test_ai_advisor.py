"""Tests for AI Advisor service and provider types."""

import pytest

from app.services.ai_providers.base import AiProviderError, AiProviderResult


class TestAiProviderResult:
    def test_create_result(self):
        result = AiProviderResult(
            answer="test answer",
            provider="openai",
            model="gpt-4o",
            usage={"input_tokens": 100, "output_tokens": 50},
            elapsed_ms=1500,
        )
        assert result.answer == "test answer"
        assert result.provider == "openai"
        assert result.model == "gpt-4o"
        assert result.usage == {"input_tokens": 100, "output_tokens": 50}
        assert result.elapsed_ms == 1500

    def test_create_result_without_usage(self):
        result = AiProviderResult(
            answer="test",
            provider="gemini",
            model="gemini-2.5-flash",
            usage=None,
            elapsed_ms=2000,
        )
        assert result.usage is None


class TestAiProviderError:
    def test_error_with_detail(self):
        err = AiProviderError(
            user_message="요청 한도 초과. 잠시 후 다시 시도해주세요.",
            detail="429 Too Many Requests from OpenAI",
        )
        assert err.user_message == "요청 한도 초과. 잠시 후 다시 시도해주세요."
        assert err.detail == "429 Too Many Requests from OpenAI"
        assert str(err) == "요청 한도 초과. 잠시 후 다시 시도해주세요."

    def test_error_without_detail(self):
        err = AiProviderError(user_message="일반 오류")
        assert err.detail == ""


import time
from unittest.mock import AsyncMock, MagicMock, patch


class TestOpenAIProvider:
    def test_init_defaults(self):
        from app.services.ai_providers.openai_provider import OpenAIProvider

        provider = OpenAIProvider(api_key="test-key")
        assert provider.provider_name == "openai"
        assert provider.default_model == "gpt-4o"

    def test_init_grok(self):
        from app.services.ai_providers.openai_provider import OpenAIProvider

        provider = OpenAIProvider(
            api_key="xai-key",
            base_url="https://api.x.ai/v1",
            provider_name="grok",
            default_model="grok-3-mini",
        )
        assert provider.provider_name == "grok"
        assert provider.default_model == "grok-3-mini"

    @pytest.mark.asyncio
    async def test_ask_success(self):
        from app.services.ai_providers.openai_provider import OpenAIProvider

        provider = OpenAIProvider(api_key="test-key")

        mock_choice = MagicMock()
        mock_choice.message.content = "AI 분석 결과입니다."

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 100
        mock_usage.completion_tokens = 50

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.model = "gpt-4o-2024-08-06"
        mock_response.usage = mock_usage

        provider.client = AsyncMock()
        provider.client.chat.completions.create = AsyncMock(return_value=mock_response)

        result = await provider.ask(
            system_prompt="system",
            user_message="질문",
            model="gpt-4o",
            timeout=30.0,
        )

        assert result.answer == "AI 분석 결과입니다."
        assert result.provider == "openai"
        assert result.model == "gpt-4o-2024-08-06"
        assert result.usage == {"input_tokens": 100, "output_tokens": 50}
        assert result.elapsed_ms >= 0

    @pytest.mark.asyncio
    async def test_ask_rate_limit_error(self):
        from openai import RateLimitError

        from app.services.ai_providers.openai_provider import OpenAIProvider

        provider = OpenAIProvider(api_key="test-key")
        provider.client = AsyncMock()

        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {}
        mock_response.json.return_value = {"error": {"message": "Rate limit exceeded"}}
        provider.client.chat.completions.create = AsyncMock(
            side_effect=RateLimitError(
                message="Rate limit exceeded",
                response=mock_response,
                body={"error": {"message": "Rate limit exceeded"}},
            )
        )

        with pytest.raises(AiProviderError) as exc_info:
            await provider.ask(system_prompt="s", user_message="q")

        assert "한도 초과" in exc_info.value.user_message

    @pytest.mark.asyncio
    async def test_ask_auth_error(self):
        from openai import AuthenticationError

        from app.services.ai_providers.openai_provider import OpenAIProvider

        provider = OpenAIProvider(api_key="bad-key")
        provider.client = AsyncMock()

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.headers = {}
        mock_response.json.return_value = {"error": {"message": "Invalid API key"}}
        provider.client.chat.completions.create = AsyncMock(
            side_effect=AuthenticationError(
                message="Invalid API key",
                response=mock_response,
                body={"error": {"message": "Invalid API key"}},
            )
        )

        with pytest.raises(AiProviderError) as exc_info:
            await provider.ask(system_prompt="s", user_message="q")

        assert "인증 실패" in exc_info.value.user_message


class TestGeminiProvider:
    def test_init_defaults(self):
        from app.services.ai_providers.gemini_provider import GeminiProvider

        with patch("app.services.ai_providers.gemini_provider.genai") as mock_genai:
            provider = GeminiProvider(api_key="test-key")
            assert provider.provider_name == "gemini"
            assert provider.default_model == "gemini-2.5-flash"
            mock_genai.Client.assert_called_once_with(api_key="test-key")

    @pytest.mark.asyncio
    async def test_ask_success(self):
        from app.services.ai_providers.gemini_provider import GeminiProvider

        with patch("app.services.ai_providers.gemini_provider.genai"):
            provider = GeminiProvider(api_key="test-key")

        mock_usage = MagicMock()
        mock_usage.prompt_token_count = 200
        mock_usage.candidates_token_count = 100

        mock_response = MagicMock()
        mock_response.text = "Gemini 분석 결과입니다."
        mock_response.usage_metadata = mock_usage
        mock_response.model_version = "gemini-2.5-flash-preview-04-17"

        provider.client = MagicMock()
        provider.client.aio.models.generate_content = AsyncMock(
            return_value=mock_response
        )

        result = await provider.ask(
            system_prompt="system",
            user_message="질문",
        )

        assert result.answer == "Gemini 분석 결과입니다."
        assert result.provider == "gemini"
        assert result.usage == {"input_tokens": 200, "output_tokens": 100}

    @pytest.mark.asyncio
    async def test_ask_error_maps_to_provider_error(self):
        from app.services.ai_providers.gemini_provider import GeminiProvider

        with patch("app.services.ai_providers.gemini_provider.genai"):
            provider = GeminiProvider(api_key="test-key")

        provider.client = MagicMock()
        provider.client.aio.models.generate_content = AsyncMock(
            side_effect=Exception("API error")
        )

        with pytest.raises(AiProviderError) as exc_info:
            await provider.ask(system_prompt="s", user_message="q")

        assert "실패" in exc_info.value.user_message


from app.schemas.ai_markdown import PresetType


class TestAiAdvisorSchemas:
    def test_request_portfolio_scope(self):
        from app.schemas.ai_advisor import AiAdviceRequest

        req = AiAdviceRequest(
            scope="portfolio",
            preset=PresetType.PORTFOLIO_STANCE,
            provider="gemini",
            question="비중 조절 필요한 종목은?",
        )
        assert req.scope == "portfolio"
        assert req.include_market == "ALL"

    def test_request_position_scope(self):
        from app.schemas.ai_advisor import AiAdviceRequest

        req = AiAdviceRequest(
            scope="position",
            preset=PresetType.STOCK_STANCE,
            provider="openai",
            question="추가매수 조건 정리해줘",
            market_type="US",
            symbol="AAPL",
        )
        assert req.scope == "position"
        assert req.market_type == "US"
        assert req.symbol == "AAPL"

    def test_response_success(self):
        from app.schemas.ai_advisor import AiAdviceResponse

        resp = AiAdviceResponse(
            success=True,
            answer="분석 결과",
            provider="gemini",
            model="gemini-2.5-flash",
            elapsed_ms=3000,
        )
        assert resp.success is True
        assert resp.error is None
        assert resp.disclaimer == "AI 분석 보조 도구이며 투자 자문이 아닙니다."

    def test_response_failure(self):
        from app.schemas.ai_advisor import AiAdviceResponse

        resp = AiAdviceResponse(
            success=False,
            answer="",
            provider="openai",
            model="",
            elapsed_ms=100,
            error="요청 한도 초과",
        )
        assert resp.success is False
        assert resp.error == "요청 한도 초과"

    def test_providers_response(self):
        from app.schemas.ai_advisor import AiProvidersResponse, ProviderInfo

        resp = AiProvidersResponse(
            providers=[
                ProviderInfo(name="gemini", default_model="gemini-2.5-flash"),
                ProviderInfo(name="openai", default_model="gpt-4o"),
            ],
            default_provider="gemini",
        )
        assert len(resp.providers) == 2
        assert resp.default_provider == "gemini"


class TestExtractContextBeforeQuestion:
    def test_splits_at_question_marker(self):
        from app.services.ai_advisor_service import extract_context_before_question

        content = "# 제목\n\n## 투자 성향\n내용\n\n## 질문\n질문 내용\n\n## 원하는 답변 형식\n형식"
        result = extract_context_before_question(content)
        assert result == "# 제목\n\n## 투자 성향\n내용"
        assert "## 질문" not in result

    def test_returns_full_content_when_no_marker(self):
        from app.services.ai_advisor_service import extract_context_before_question

        content = "# 제목\n\n## 투자 성향\n내용만 있음"
        result = extract_context_before_question(content)
        assert result == content

    def test_empty_content(self):
        from app.services.ai_advisor_service import extract_context_before_question

        assert extract_context_before_question("") == ""


class TestAiAdvisorService:
    @pytest.fixture
    def mock_markdown_service(self):
        service = MagicMock()
        service.generate_portfolio_stance_markdown.return_value = {
            "content": "# 포트폴리오\n\n## 투자 성향\n내용\n\n## 질문\n기존 질문",
            "title": "test",
            "filename": "test.md",
            "metadata": {},
        }
        service.generate_stock_stance_markdown.return_value = {
            "content": "# AAPL 스탠스\n\n## 현재 포지션\n내용\n\n## 질문\n기존 질문",
            "title": "test",
            "filename": "test.md",
            "metadata": {},
        }
        service.generate_stock_add_or_hold_markdown.return_value = {
            "content": "# AAPL 추가매수\n\n## 현재 포지션\n내용\n\n## 질문\n기존 질문",
            "title": "test",
            "filename": "test.md",
            "metadata": {},
        }
        return service

    @pytest.fixture
    def mock_overview_service(self):
        service = AsyncMock()
        service.get_overview.return_value = {
            "success": True,
            "positions": [{"symbol": "AAPL", "name": "Apple"}],
        }
        return service

    @pytest.fixture
    def mock_detail_service(self):
        service = AsyncMock()
        service.get_page_payload.return_value = {
            "summary": {"symbol": "AAPL", "name": "Apple", "market_type": "US"},
            "weights": {},
            "journal": {},
        }
        return service

    @pytest.fixture
    def advisor_service(
        self, mock_markdown_service, mock_overview_service, mock_detail_service
    ):
        from app.services.ai_advisor_service import AiAdvisorService

        service = AiAdvisorService(
            markdown_service=mock_markdown_service,
            overview_service=mock_overview_service,
            detail_service=mock_detail_service,
        )
        return service

    def test_no_providers_when_no_keys(self, advisor_service):
        assert advisor_service.available_providers() == []

    def test_registers_provider_when_key_set(
        self, mock_markdown_service, mock_overview_service, mock_detail_service
    ):
        from app.services.ai_advisor_service import (
            AiAdvisorService,
            get_configured_providers,
        )
        from app.services.ai_providers.openai_provider import OpenAIProvider

        fake_providers = {"openai": OpenAIProvider(api_key="sk-test")}
        with patch(
            "app.services.ai_advisor_service.get_configured_providers",
            return_value=fake_providers,
        ):
            service = AiAdvisorService(
                markdown_service=mock_markdown_service,
                overview_service=mock_overview_service,
                detail_service=mock_detail_service,
            )
            providers = service.available_providers()
            assert len(providers) == 1
            assert providers[0]["name"] == "openai"

    @pytest.mark.asyncio
    async def test_ask_portfolio_scope(self, advisor_service, mock_overview_service):
        mock_provider = AsyncMock()
        mock_provider.provider_name = "test"
        mock_provider.default_model = "test-model"
        mock_provider.ask.return_value = AiProviderResult(
            answer="분석 결과",
            provider="test",
            model="test-model",
            usage=None,
            elapsed_ms=1000,
        )
        advisor_service.providers["test"] = mock_provider

        result = await advisor_service.ask(
            user_id=1,
            scope="portfolio",
            preset=PresetType.PORTFOLIO_STANCE,
            provider="test",
            question="비중 조절 필요한 종목?",
        )

        assert result.success is True
        assert result.answer == "분석 결과"
        mock_overview_service.get_overview.assert_awaited_once()

        # Verify context was stripped of ## 질문
        call_args = mock_provider.ask.call_args
        system_prompt = call_args.kwargs.get("system_prompt") or call_args[0][0]
        assert "## 질문" not in system_prompt
        assert "투자 성향" in system_prompt

    @pytest.mark.asyncio
    async def test_ask_position_scope_add_or_hold(
        self, advisor_service, mock_detail_service
    ):
        mock_provider = AsyncMock()
        mock_provider.provider_name = "test"
        mock_provider.default_model = "test-model"
        mock_provider.ask.return_value = AiProviderResult(
            answer="추가매수 분석",
            provider="test",
            model="test-model",
            usage=None,
            elapsed_ms=500,
        )
        advisor_service.providers["test"] = mock_provider

        result = await advisor_service.ask(
            user_id=1,
            scope="position",
            preset=PresetType.STOCK_ADD_OR_HOLD,
            provider="test",
            question="추가매수 해도 될까?",
            market_type="US",
            symbol="AAPL",
        )

        assert result.success is True
        assert result.answer == "추가매수 분석"
        mock_detail_service.get_page_payload.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ask_provider_error_propagates(self, advisor_service):
        mock_provider = AsyncMock()
        mock_provider.provider_name = "test"
        mock_provider.default_model = "test-model"
        mock_provider.ask.side_effect = AiProviderError(
            user_message="요청 한도 초과",
            detail="429",
        )
        advisor_service.providers["test"] = mock_provider

        with pytest.raises(AiProviderError) as exc_info:
            await advisor_service.ask(
                user_id=1,
                scope="portfolio",
                preset=PresetType.PORTFOLIO_STANCE,
                provider="test",
                question="질문",
            )

        assert "한도 초과" in exc_info.value.user_message
