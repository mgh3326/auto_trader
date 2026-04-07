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
