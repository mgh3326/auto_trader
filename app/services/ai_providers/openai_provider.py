"""OpenAI-compatible provider adapter (GPT + Grok)."""

from __future__ import annotations

import logging
import time

from openai import (
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    RateLimitError,
)

from app.services.ai_providers.base import AiProviderError, AiProviderResult

logger = logging.getLogger(__name__)


class OpenAIProvider:
    """Adapter for OpenAI-compatible APIs (GPT, Grok via base_url)."""

    def __init__(
        self,
        api_key: str,
        provider_name: str = "openai",
        default_model: str = "gpt-4o",
        base_url: str | None = None,
    ) -> None:
        self.provider_name = provider_name
        self.default_model = default_model
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def ask(
        self,
        system_prompt: str,
        user_message: str,
        model: str | None = None,
        timeout: float = 60.0,
    ) -> AiProviderResult:
        model = model or self.default_model
        start = time.monotonic()
        try:
            response = await self.client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                timeout=timeout,
            )
            elapsed_ms = int((time.monotonic() - start) * 1000)

            answer = response.choices[0].message.content or ""
            usage = None
            if response.usage:
                usage = {
                    "input_tokens": response.usage.prompt_tokens,
                    "output_tokens": response.usage.completion_tokens,
                }

            return AiProviderResult(
                answer=answer,
                provider=self.provider_name,
                model=response.model,
                usage=usage,
                elapsed_ms=elapsed_ms,
            )
        except RateLimitError as e:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.warning("AI provider %s rate limited: %s", self.provider_name, e)
            raise AiProviderError(
                user_message="요청 한도 초과. 잠시 후 다시 시도해주세요.",
                detail=str(e),
            ) from e
        except APITimeoutError as e:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.warning("AI provider %s timeout: %s", self.provider_name, e)
            raise AiProviderError(
                user_message="응답 시간 초과. 다른 모델이나 짧은 질문으로 다시 시도해주세요.",
                detail=str(e),
            ) from e
        except AuthenticationError as e:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.error("AI provider %s auth error: %s", self.provider_name, e)
            raise AiProviderError(
                user_message="API 인증 실패. 설정을 확인해주세요.",
                detail=str(e),
            ) from e
        except Exception as e:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.error("AI provider %s error: %s", self.provider_name, e, exc_info=True)
            raise AiProviderError(
                user_message="AI 응답 생성 실패. 잠시 후 다시 시도해주세요.",
                detail=str(e),
            ) from e
