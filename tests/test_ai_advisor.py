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
