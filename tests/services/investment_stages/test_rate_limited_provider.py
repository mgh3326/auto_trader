"""Tests for RateLimitedGeminiProvider (C1, ROB-279)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.ai_providers.base import AiProviderError, AiProviderResult
from app.services.investment_stages.rate_limited_provider import (
    RateLimitedGeminiProvider,
)


def _make_provider(model: str = "gemini-2.5-flash") -> MagicMock:
    provider = MagicMock()
    provider.provider_name = "gemini"
    provider.default_model = model
    provider.ask = AsyncMock(
        return_value=AiProviderResult(
            answer="ok",
            provider="gemini",
            model=model,
            usage=None,
            elapsed_ms=10,
        )
    )
    return provider


def _make_rate_limiter(*, limited: bool = False) -> MagicMock:
    rl = MagicMock()
    rl.is_model_limited = AsyncMock(return_value=limited)
    rl.mark_limited = AsyncMock()
    return rl


@pytest.mark.asyncio
async def test_passes_through_when_not_limited():
    provider = _make_provider()
    rl = _make_rate_limiter(limited=False)

    wrapped = RateLimitedGeminiProvider(provider, rl)
    result = await wrapped.ask(system_prompt="sys", user_message="msg")

    provider.ask.assert_called_once()
    assert result.answer == "ok"
    rl.mark_limited.assert_not_called()


@pytest.mark.asyncio
async def test_raises_without_calling_provider_when_model_is_limited():
    provider = _make_provider()
    rl = _make_rate_limiter(limited=True)

    wrapped = RateLimitedGeminiProvider(provider, rl)
    with pytest.raises(AiProviderError, match="rate-limited"):
        await wrapped.ask(system_prompt="sys", user_message="msg")

    # Provider must NOT be called when rate-limited
    provider.ask.assert_not_called()


@pytest.mark.asyncio
async def test_records_429_to_rate_limiter():
    provider = _make_provider()
    provider.ask = AsyncMock(
        side_effect=AiProviderError(
            user_message="rate limit exceeded",
            detail="429 quota exhausted",
        )
    )
    rl = _make_rate_limiter(limited=False)

    wrapped = RateLimitedGeminiProvider(provider, rl)
    with pytest.raises(AiProviderError):
        await wrapped.ask(system_prompt="sys", user_message="msg")

    rl.mark_limited.assert_awaited_once()


@pytest.mark.asyncio
async def test_does_not_record_non_429_errors():
    provider = _make_provider()
    provider.ask = AsyncMock(
        side_effect=AiProviderError(
            user_message="auth error",
            detail="401 unauthorized",
        )
    )
    rl = _make_rate_limiter(limited=False)

    wrapped = RateLimitedGeminiProvider(provider, rl)
    with pytest.raises(AiProviderError):
        await wrapped.ask(system_prompt="sys", user_message="msg")

    rl.mark_limited.assert_not_awaited()


@pytest.mark.asyncio
async def test_exposes_provider_name_and_default_model():
    provider = _make_provider(model="gemini-pro")
    rl = _make_rate_limiter()
    wrapped = RateLimitedGeminiProvider(provider, rl)
    assert wrapped.provider_name == "gemini"
    assert wrapped.default_model == "gemini-pro"
