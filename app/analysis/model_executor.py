from __future__ import annotations

import asyncio
import json

from google import genai
from google.genai.errors import ClientError
from google.genai.types import GenerateContentConfigDict, HttpOptions

from app.analysis.models import StockAnalysisResponse
from app.analysis.response_validator import ResponseValidator
from app.core.config import settings
from app.core.model_rate_limiter import ModelRateLimiter

GEMINI_TIMEOUT = 3 * 60 * 1000


class ModelExecutor:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: genai.Client | None = None,
        rate_limiter: ModelRateLimiter | None = None,
        validator: ResponseValidator | None = None,
    ) -> None:
        self.api_key = api_key or settings.get_random_key()
        self.client = client or genai.Client(
            api_key=self.api_key,
            http_options=HttpOptions(timeout=GEMINI_TIMEOUT),
        )
        self.rate_limiter = rate_limiter or ModelRateLimiter()
        self.validator = validator or ResponseValidator()

    async def close(self) -> None:
        await self.rate_limiter.close()

    def _mask_api_key(self, api_key: str) -> str:
        if not api_key or len(api_key) < 8:
            return "***"
        return f"{api_key[:4]}...{api_key[-3:]}"

    async def execute(
        self,
        prompt: str,
        max_retries: int = 3,
        *,
        use_json: bool = False,
    ) -> tuple[str | StockAnalysisResponse, str]:
        models_to_try = [
            "gemini-2.5-pro",
            "gemini-3-flash-preview",
            "gemini-2.5-flash",
            "gemini-2.5-flash-preview-09-2025",
            "gemini-2.0-flash",
        ]

        for model in models_to_try:
            print(f"모델 시도: {model}")

            for attempt in range(max_retries):
                masked_api_key = self._mask_api_key(self.api_key)
                if not await self.rate_limiter.is_model_available(model, self.api_key):
                    print(
                        f"  {model} 모델 (API: {masked_api_key}) 사용 제한 중, 다음 모델로..."
                    )
                    self.api_key = settings.get_next_key()
                    continue
                try:
                    if use_json:
                        config: GenerateContentConfigDict = {
                            "response_mime_type": "application/json",
                            "response_json_schema": StockAnalysisResponse.model_json_schema(),
                        }
                        resp = self.client.models.generate_content(
                            model=model,
                            contents=prompt,
                            config=config,
                        )
                    else:
                        resp = self.client.models.generate_content(
                            model=model,
                            contents=prompt,
                        )

                    if resp and resp.candidates:
                        candidate = resp.candidates[0]
                        finish_reason = getattr(candidate, "finish_reason", None)

                        if finish_reason == "STOP" and not resp.text:
                            print(
                                f"  시도 {attempt + 1}: STOP이지만 text가 None, 재시도..."
                            )
                            await asyncio.sleep(1 + attempt)
                            continue

                        if finish_reason in ["SAFETY", "RECITATION"]:
                            print(
                                f"  {model} 차단됨: {finish_reason}, 다음 모델로 시도..."
                            )
                            break

                        if resp.text:
                            print(f"  {model} 성공!")
                            if use_json:
                                try:
                                    validated_response = self.validator.validate(
                                        resp.text,
                                        use_json=True,
                                    )
                                    return validated_response, model
                                except (json.JSONDecodeError, ValueError) as error:
                                    print(f"  JSON 파싱/검증 실패: {error}")
                                    if attempt < max_retries - 1:
                                        await asyncio.sleep(1 + attempt)
                                        continue
                                    return resp.text, model
                            return resp.text, model

                except ClientError as error:
                    if error.code == 429:
                        print(f"  {model} 모델 할당량 초과(429), Redis에 제한 설정...")

                        retry_delay = None
                        raw_details = getattr(error, "details", None)
                        if raw_details:
                            try:
                                if isinstance(raw_details, dict):
                                    detail_items = raw_details.get("error", {}).get(
                                        "details", []
                                    )
                                elif isinstance(raw_details, list):
                                    detail_items = raw_details
                                else:
                                    detail_items = []

                                for detail in detail_items:
                                    if not isinstance(detail, dict):
                                        continue
                                    if (
                                        "@type" in detail
                                        and detail["@type"]
                                        == "type.googleapis.com/google.rpc.RetryInfo"
                                    ):
                                        retry_delay = detail.get("retryDelay")
                                        if retry_delay:
                                            print(f"  retry_delay: {retry_delay}")
                                            await (
                                                self.rate_limiter.set_model_rate_limit(
                                                    model,
                                                    self.api_key,
                                                    retry_delay,
                                                    error.code,
                                                )
                                            )
                            except Exception as parse_error:
                                print(f"  retry_delay 파싱 오류: {parse_error}")

                        if not retry_delay:
                            await self.rate_limiter.set_model_rate_limit(
                                model,
                                self.api_key,
                                {"seconds": 60},
                                error.code,
                            )

                        new_api_key = settings.get_next_key()
                        print(
                            f"  API 키 교체: {masked_api_key} → {self._mask_api_key(new_api_key)}"
                        )
                        self.client = genai.Client(
                            api_key=new_api_key,
                            http_options=HttpOptions(timeout=GEMINI_TIMEOUT),
                        )
                        self.api_key = new_api_key

                        print("  새로운 API 키로 계속 시도...")
                        if model == "gemini-2.5-pro":
                            print("pro 는 다음 모델로 실행")
                            break
                        continue

                    print(
                        f"  시도 {attempt + 1} 실패 (ClientError {error.code}): {error}"
                    )
                    await asyncio.sleep(2 + attempt)

                except Exception as error:
                    print(f"  시도 {attempt + 1} 실패: {error}")

                    error_str = str(error)
                    if (
                        "503" in error_str
                        and "UNAVAILABLE" in error_str
                        or "500" in error_str
                    ):
                        print(f"  {model} 모델 과부하(503), Redis에 1분 제한 설정...")
                        await self.rate_limiter.set_model_rate_limit(
                            model,
                            self.api_key,
                            {"seconds": 60},
                            503,
                        )

                        new_api_key = settings.get_next_key()
                        masked_api_key = self._mask_api_key(self.api_key)
                        print(
                            f"  API 키 교체: {masked_api_key} → {self._mask_api_key(new_api_key)}"
                        )
                        self.client = genai.Client(
                            api_key=new_api_key,
                            http_options=HttpOptions(timeout=GEMINI_TIMEOUT),
                        )
                        self.api_key = new_api_key

                        print("  새로운 API 키로 계속 시도...")
                        if model == "gemini-2.5-pro":
                            print("pro 는 다음 모델로 실행")
                            break
                        continue

                    await asyncio.sleep(2 + attempt)
            else:
                print(f"  {model} 모든 시도 실패, 다음 모델로...")
                continue

        return "모든 모델과 재시도 실패", "N/A"
