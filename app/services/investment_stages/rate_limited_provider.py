"""Rate-limited LLM provider wrapper for staged report pipeline (ROB-279).

Wraps GeminiProvider with ModelRateLimiter consultation per ROB-279 spec.
Falls back to next model on rate-limited; records 429s.
"""

from __future__ import annotations

import logging

from app.core.model_rate_limiter import ModelRateLimiter
from app.services.ai_providers.base import AiProviderError, AiProviderResult
from app.services.ai_providers.gemini_provider import GeminiProvider

_logger = logging.getLogger(__name__)


class RateLimitedGeminiProvider:
    """Thin facade: same .ask(system_prompt, user_message, ...) signature as
    GeminiProvider, but consults ModelRateLimiter before/after each call.
    Used by stage reducers and the final composer.
    """

    def __init__(
        self,
        provider: GeminiProvider,
        rate_limiter: ModelRateLimiter | None = None,
    ) -> None:
        self._provider = provider
        self._rate_limiter = rate_limiter or ModelRateLimiter()

    # Expose provider_name / default_model so callers that duck-type the
    # underlying GeminiProvider still work.
    @property
    def provider_name(self) -> str:
        return self._provider.provider_name

    @property
    def default_model(self) -> str:
        return self._provider.default_model

    async def ask(
        self,
        system_prompt: str,
        user_message: str,
        model: str | None = None,
        timeout: float = 60.0,
    ) -> AiProviderResult:
        model_name = model or self._provider.default_model
        # Use a short, non-secret hint derived from the model name for Redis keys.
        api_key_hint = model_name

        if await self._rate_limiter.is_model_limited(model_name, api_key_hint):
            _logger.warning(
                "rob-279 rate_limiter: model %s rate-limited; raising AiProviderError",
                model_name,
            )
            raise AiProviderError(
                user_message=f"model {model_name} rate-limited per ModelRateLimiter",
                detail="rate-limited",
            )

        try:
            return await self._provider.ask(
                system_prompt=system_prompt,
                user_message=user_message,
                model=model,
                timeout=timeout,
            )
        except AiProviderError as exc:
            # Detect 429 / quota-exceeded and record to rate limiter
            msg = (exc.detail or str(exc)).lower()
            if "429" in msg or "quota" in msg or "rate" in msg or "resource_exhausted" in msg:
                await self._rate_limiter.mark_limited(model_name, api_key_hint)
            raise
