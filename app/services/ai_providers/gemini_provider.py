"""Google Gemini provider adapter."""

from __future__ import annotations

import logging
import time

from google import genai
from google.genai import types

from app.services.ai_providers.base import AiProviderError, AiProviderResult

logger = logging.getLogger(__name__)


class GeminiProvider:
    """Adapter for Google Gemini API."""

    def __init__(
        self,
        api_key: str,
        default_model: str = "gemini-2.5-flash",
    ) -> None:
        self.provider_name = "gemini"
        self.default_model = default_model
        self.client = genai.Client(api_key=api_key)

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
            response = await self.client.aio.models.generate_content(
                model=model,
                contents=user_message,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    http_options=types.HttpOptions(timeout=int(timeout * 1000)),
                ),
            )
            elapsed_ms = int((time.monotonic() - start) * 1000)

            answer = response.text or ""
            usage = None
            if response.usage_metadata:
                usage = {
                    "input_tokens": response.usage_metadata.prompt_token_count,
                    "output_tokens": response.usage_metadata.candidates_token_count,
                }

            model_version = getattr(response, "model_version", model)

            return AiProviderResult(
                answer=answer,
                provider=self.provider_name,
                model=model_version or model,
                usage=usage,
                elapsed_ms=elapsed_ms,
            )
        except Exception as e:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            error_str = str(e).lower()

            if "429" in error_str or "resource_exhausted" in error_str:
                logger.warning("Gemini rate limited: %s", e)
                raise AiProviderError(
                    user_message="요청 한도 초과. 잠시 후 다시 시도해주세요.",
                    detail=str(e),
                ) from e
            if "401" in error_str or "403" in error_str or "api_key" in error_str:
                logger.error("Gemini auth error: %s", e)
                raise AiProviderError(
                    user_message="API 인증 실패. 설정을 확인해주세요.",
                    detail=str(e),
                ) from e
            if "timeout" in error_str or "deadline" in error_str:
                logger.warning("Gemini timeout: %s", e)
                raise AiProviderError(
                    user_message="응답 시간 초과. 다른 모델이나 짧은 질문으로 다시 시도해주세요.",
                    detail=str(e),
                ) from e

            logger.error("Gemini error: %s", e, exc_info=True)
            raise AiProviderError(
                user_message="AI 응답 생성 실패. 잠시 후 다시 시도해주세요.",
                detail=str(e),
            ) from e
