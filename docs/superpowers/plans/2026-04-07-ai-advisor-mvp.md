# AI Advisor MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add AI advisor endpoints and UI that reuse existing AIMarkdownService context to query external LLM providers (GPT, Gemini, Grok) from the portfolio and position detail pages.

**Architecture:** New `AiAdvisorService` orchestrates: (1) call existing `AIMarkdownService` to generate markdown context, (2) strip the `## 질문` section, (3) wrap in a system prompt, (4) dispatch to the selected provider adapter. Two thin provider adapters: `OpenAIProvider` (covers GPT + Grok via base_url) and `GeminiProvider`. Two new endpoints on the existing portfolio router. Collapsible UI panel on both templates.

**Tech Stack:** FastAPI, openai SDK, google-genai SDK, Pydantic, Bootstrap 5, marked.js (CDN)

**Spec:** `docs/superpowers/specs/2026-04-07-ai-advisor-mvp-design.md`

---

### Task 1: Config + Dependencies

**Files:**
- Modify: `pyproject.toml:10-48` (dependencies list)
- Modify: `app/core/config.py:397-414` (before `model_config`)
- Modify: `env.example:294` (append at end)
- Modify: `tests/conftest.py:46-75` (default_env_values dict)

- [ ] **Step 1: Add SDK dependencies to pyproject.toml**

In `pyproject.toml`, add these two lines to the `dependencies` array (after the `tenacity` line, before the closing `]`):

```toml
    "openai>=1.82.0,<2.0.0",
    "google-genai>=1.16.0,<2.0.0",
```

- [ ] **Step 2: Add config settings to Settings class**

In `app/core/config.py`, add these fields just before the `model_config = SettingsConfigDict(...)` block (around line 406):

```python
    # AI Advisor
    openai_api_key: str | None = None
    gemini_advisor_api_key: str | None = None
    grok_api_key: str | None = None
    ai_advisor_timeout: float = 60.0
    ai_advisor_default_provider: str = "gemini"
```

- [ ] **Step 3: Add env vars to env.example**

Append to the end of `env.example`:

```bash

# AI Advisor (optional - for portfolio AI consultation)
OPENAI_API_KEY=
GEMINI_ADVISOR_API_KEY=
GROK_API_KEY=
AI_ADVISOR_TIMEOUT=60.0
AI_ADVISOR_DEFAULT_PROVIDER=gemini
```

- [ ] **Step 4: Add test env defaults to conftest.py**

In `tests/conftest.py`, add these entries to the `default_env_values` dict (around line 74, before the closing `}`):

```python
        "OPENAI_API_KEY": "",
        "GEMINI_ADVISOR_API_KEY": "",
        "GROK_API_KEY": "",
        "AI_ADVISOR_TIMEOUT": "60.0",
        "AI_ADVISOR_DEFAULT_PROVIDER": "gemini",
```

- [ ] **Step 5: Install dependencies**

Run: `uv sync`
Expected: Resolves and installs openai + google-genai without conflicts.

- [ ] **Step 6: Verify config loads**

Run: `uv run python -c "from app.core.config import settings; print(settings.ai_advisor_timeout)"`
Expected: `60.0`

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml app/core/config.py env.example tests/conftest.py uv.lock
git commit -m "feat(ai-advisor): add config settings and SDK dependencies"
```

---

### Task 2: Provider Base Types

**Files:**
- Create: `app/services/ai_providers/__init__.py`
- Create: `app/services/ai_providers/base.py`
- Create: `tests/test_ai_advisor.py`

- [ ] **Step 1: Write tests for base types**

Create `tests/test_ai_advisor.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ai_advisor.py::TestAiProviderResult -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.ai_providers'`

- [ ] **Step 3: Create package and base module**

Create `app/services/ai_providers/__init__.py`:

```python
```

Create `app/services/ai_providers/base.py`:

```python
"""AI Provider base types."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel


class AiProviderResult(BaseModel):
    """Result from an AI provider call."""

    answer: str
    provider: str
    model: str
    usage: dict[str, Any] | None
    elapsed_ms: int


class AiProviderError(Exception):
    """Provider call error with user-facing message and internal detail."""

    def __init__(self, user_message: str, detail: str = "") -> None:
        self.user_message = user_message
        self.detail = detail
        super().__init__(user_message)


class AiProvider(Protocol):
    """Protocol for AI provider adapters."""

    provider_name: str
    default_model: str

    async def ask(
        self,
        system_prompt: str,
        user_message: str,
        model: str | None = None,
        timeout: float = 60.0,
    ) -> AiProviderResult: ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ai_advisor.py::TestAiProviderResult tests/test_ai_advisor.py::TestAiProviderError -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/ai_providers/ tests/test_ai_advisor.py
git commit -m "feat(ai-advisor): add provider base types and protocol"
```

---

### Task 3: OpenAI Provider Adapter

**Files:**
- Create: `app/services/ai_providers/openai_provider.py`
- Modify: `tests/test_ai_advisor.py`

- [ ] **Step 1: Write tests for OpenAI provider**

Append to `tests/test_ai_advisor.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ai_advisor.py::TestOpenAIProvider::test_init_defaults -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.ai_providers.openai_provider'`

- [ ] **Step 3: Implement OpenAI provider**

Create `app/services/ai_providers/openai_provider.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ai_advisor.py::TestOpenAIProvider -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/ai_providers/openai_provider.py tests/test_ai_advisor.py
git commit -m "feat(ai-advisor): add OpenAI provider adapter"
```

---

### Task 4: Gemini Provider Adapter

**Files:**
- Create: `app/services/ai_providers/gemini_provider.py`
- Modify: `tests/test_ai_advisor.py`

- [ ] **Step 1: Write tests for Gemini provider**

Append to `tests/test_ai_advisor.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ai_advisor.py::TestGeminiProvider::test_init_defaults -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.ai_providers.gemini_provider'`

- [ ] **Step 3: Implement Gemini provider**

Create `app/services/ai_providers/gemini_provider.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ai_advisor.py::TestGeminiProvider -v`
Expected: All 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/ai_providers/gemini_provider.py tests/test_ai_advisor.py
git commit -m "feat(ai-advisor): add Gemini provider adapter"
```

---

### Task 5: Schemas

**Files:**
- Create: `app/schemas/ai_advisor.py`
- Modify: `tests/test_ai_advisor.py`

- [ ] **Step 1: Write tests for schemas**

Append to `tests/test_ai_advisor.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ai_advisor.py::TestAiAdvisorSchemas::test_request_portfolio_scope -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.schemas.ai_advisor'`

- [ ] **Step 3: Create schemas**

Create `app/schemas/ai_advisor.py`:

```python
"""AI Advisor request/response schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.ai_markdown import PresetType

DISCLAIMER = "AI 분석 보조 도구이며 투자 자문이 아닙니다."


class AiAdviceRequest(BaseModel):
    """AI advice request."""

    scope: Literal["portfolio", "position"]
    preset: PresetType
    provider: str
    model: str | None = None
    question: str = Field(..., min_length=1, max_length=2000)
    # position scope
    market_type: str | None = None
    symbol: str | None = None
    # portfolio scope
    include_market: str = "ALL"


class AiAdviceResponse(BaseModel):
    """AI advice response."""

    success: bool
    answer: str
    provider: str
    model: str
    usage: dict[str, Any] | None = None
    elapsed_ms: int
    error: str | None = None
    disclaimer: str = DISCLAIMER


class ProviderInfo(BaseModel):
    """Single provider info."""

    name: str
    default_model: str


class AiProvidersResponse(BaseModel):
    """Available providers response."""

    providers: list[ProviderInfo]
    default_provider: str
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ai_advisor.py::TestAiAdvisorSchemas -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/ai_advisor.py tests/test_ai_advisor.py
git commit -m "feat(ai-advisor): add request/response schemas"
```

---

### Task 6: AiAdvisorService

**Files:**
- Create: `app/services/ai_advisor_service.py`
- Modify: `tests/test_ai_advisor.py`

- [ ] **Step 1: Write tests for extract_context_before_question helper**

Append to `tests/test_ai_advisor.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ai_advisor.py::TestExtractContextBeforeQuestion -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.ai_advisor_service'`

- [ ] **Step 3: Write tests for AiAdvisorService**

Append to `tests/test_ai_advisor.py`:

```python
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
        from app.services.ai_advisor_service import AiAdvisorService
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
```

- [ ] **Step 4: Implement AiAdvisorService**

Create `app/services/ai_advisor_service.py`:

```python
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
```

- [ ] **Step 5: Run all tests to verify they pass**

Run: `uv run pytest tests/test_ai_advisor.py -v`
Expected: All tests PASS (base types + OpenAI + Gemini + schemas + helper + service).

- [ ] **Step 6: Commit**

```bash
git add app/services/ai_advisor_service.py tests/test_ai_advisor.py
git commit -m "feat(ai-advisor): add AiAdvisorService orchestrator"
```

---

### Task 7: API Endpoints

**Files:**
- Modify: `app/routers/portfolio.py:1-42` (imports) and append after line 444
- Modify: `tests/test_ai_advisor.py`

- [ ] **Step 1: Write tests for endpoints**

Append to `tests/test_ai_advisor.py`:

```python
from fastapi.testclient import TestClient


class TestAiAdvisorEndpoints:
    @pytest.fixture
    def client(self):
        from app.core.db import get_db
        from app.main import api
        from app.routers.dependencies import get_authenticated_user

        mock_user = MagicMock()
        mock_user.id = 1

        async def override_get_db():
            yield AsyncMock()

        api.dependency_overrides[get_db] = override_get_db
        api.dependency_overrides[get_authenticated_user] = lambda: mock_user

        yield TestClient(api)

        del api.dependency_overrides[get_db]
        del api.dependency_overrides[get_authenticated_user]

    def test_get_providers(self, client):
        response = client.get("/portfolio/api/ai-advice/providers")
        assert response.status_code == 200
        data = response.json()
        assert "providers" in data
        assert "default_provider" in data
        assert isinstance(data["providers"], list)

    def test_post_advice_invalid_scope(self, client):
        response = client.post(
            "/portfolio/api/ai-advice",
            json={
                "scope": "invalid",
                "preset": "portfolio_stance",
                "provider": "gemini",
                "question": "test",
            },
        )
        assert response.status_code == 422

    def test_post_advice_unknown_provider(self, client):
        response = client.post(
            "/portfolio/api/ai-advice",
            json={
                "scope": "portfolio",
                "preset": "portfolio_stance",
                "provider": "nonexistent",
                "question": "test",
            },
        )
        assert response.status_code == 400

    def test_post_advice_empty_question(self, client):
        response = client.post(
            "/portfolio/api/ai-advice",
            json={
                "scope": "portfolio",
                "preset": "portfolio_stance",
                "provider": "gemini",
                "question": "",
            },
        )
        assert response.status_code == 422
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ai_advisor.py::TestAiAdvisorEndpoints::test_get_providers -v`
Expected: FAIL — 404 (endpoint doesn't exist yet).

- [ ] **Step 3: Add endpoints to portfolio router**

In `app/routers/portfolio.py`, add these imports at the top (after the existing imports):

```python
from app.schemas.ai_advisor import (
    AiAdviceRequest,
    AiAdviceResponse,
    AiProvidersResponse,
    DISCLAIMER,
    ProviderInfo,
)
from app.services.ai_advisor_service import AiAdvisorService
from app.services.ai_markdown_service import AIMarkdownService
from app.services.ai_providers.base import AiProviderError
```

Then append these endpoints at the end of the file (after the `get_position_opinions` endpoint):

```python
# --- AI Advisor ---


def get_ai_advisor_service(
    db: AsyncSession = Depends(get_db),
) -> AiAdvisorService:
    overview_service = PortfolioOverviewService(db)
    dashboard_service = PortfolioDashboardService(db)
    detail_service = PortfolioPositionDetailService(
        overview_service=overview_service,
        dashboard_service=dashboard_service,
    )
    return AiAdvisorService(
        markdown_service=AIMarkdownService(),
        overview_service=overview_service,
        detail_service=detail_service,
    )


@router.get("/api/ai-advice/providers", response_model=AiProvidersResponse)
async def get_ai_providers(
    current_user: User = Depends(get_authenticated_user),
):
    from app.core.config import settings
    from app.services.ai_advisor_service import get_configured_providers

    providers = [
        ProviderInfo(name=name, default_model=p.default_model)
        for name, p in get_configured_providers().items()
    ]
    return AiProvidersResponse(
        providers=providers,
        default_provider=settings.ai_advisor_default_provider,
    )


@router.post("/api/ai-advice", response_model=AiAdviceResponse)
async def post_ai_advice(
    request: AiAdviceRequest,
    current_user: User = Depends(get_authenticated_user),
    advisor_service: AiAdvisorService = Depends(get_ai_advisor_service),
):
    import time

    start = time.monotonic()

    # Validate provider exists
    available = {p["name"] for p in advisor_service.available_providers()}
    if request.provider not in available:
        raise HTTPException(
            status_code=400,
            detail=f"Provider '{request.provider}' is not available. "
            f"Available: {sorted(available) or 'none (no API keys configured)'}",
        )

    # Validate position scope has required fields
    if request.scope == "position":
        if not request.market_type or not request.symbol:
            raise HTTPException(
                status_code=400,
                detail="market_type and symbol are required for position scope",
            )

    try:
        return await advisor_service.ask(
            user_id=current_user.id,
            scope=request.scope,
            preset=request.preset,
            provider=request.provider,
            question=request.question,
            model=request.model,
            market_type=request.market_type,
            symbol=request.symbol,
            include_market=request.include_market,
        )
    except AiProviderError as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.warning("AI advisor provider error: %s | %s", e.user_message, e.detail)
        return AiAdviceResponse(
            success=False,
            answer="",
            provider=request.provider,
            model=request.model or "",
            elapsed_ms=elapsed_ms,
            error=e.user_message,
        )
    except PortfolioPositionDetailNotFoundError:
        raise HTTPException(status_code=404, detail="Position not found")
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.error("AI advisor unexpected error: %s", e, exc_info=True)
        return AiAdviceResponse(
            success=False,
            answer="",
            provider=request.provider,
            model=request.model or "",
            elapsed_ms=elapsed_ms,
            error="AI 응답 생성 실패. 잠시 후 다시 시도해주세요.",
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ai_advisor.py::TestAiAdvisorEndpoints -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Run all AI advisor tests**

Run: `uv run pytest tests/test_ai_advisor.py -v`
Expected: All tests PASS.

- [ ] **Step 6: Run linting**

Run: `make lint`
Expected: No errors.

- [ ] **Step 7: Commit**

```bash
git add app/routers/portfolio.py tests/test_ai_advisor.py
git commit -m "feat(ai-advisor): add API endpoints for providers and advice"
```

---

### Task 8: Frontend — Position Detail Page

**Files:**
- Modify: `app/templates/portfolio_position_detail.html`

- [ ] **Step 1: Add marked.js CDN to the `<head>` section**

In `app/templates/portfolio_position_detail.html`, after the Bootstrap Icons `<link>` tag (line 9), add:

```html
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
```

- [ ] **Step 2: Add AI advisor panel HTML before the closing `</body>` tag**

In `app/templates/portfolio_position_detail.html`, insert this block just before the `</script>` that closes the main script block (before `</body>`). Insert it after the `markdownModal` event listeners (around line 995), before `</script>`:

First, add the HTML panel. Insert this just before `<script>` of the main script block — specifically, find the closing `</div>` of the main container and add the panel after it, but before `<script>`. If easier, insert before the last `<script>` block.

Insert this HTML block just before the last `<script>` tag in the file:

```html
  <!-- AI Advisor Panel -->
  <div class="ai-advisor-fab" id="advisorFab" onclick="toggleAdvisorPanel()" title="AI 상담"
       style="position:fixed;bottom:2rem;right:2rem;width:56px;height:56px;border-radius:50%;background:var(--accent,#c05a3c);color:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer;box-shadow:0 4px 12px rgba(0,0,0,0.2);z-index:1050;font-size:1.5rem;">
    <i class="bi bi-robot"></i>
  </div>

  <div class="ai-advisor-panel" id="advisorPanel"
       style="display:none;position:fixed;bottom:6rem;right:2rem;width:420px;max-height:70vh;background:var(--bg-card,#fff);border-radius:1rem;box-shadow:0 12px 40px rgba(0,0,0,0.15);z-index:1049;overflow:hidden;font-family:inherit;">
    <div style="padding:1rem 1.25rem;border-bottom:1px solid var(--border,#e5e0db);">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <strong>AI 상담</strong>
        <button onclick="toggleAdvisorPanel()" style="background:none;border:none;font-size:1.2rem;cursor:pointer;color:var(--text-secondary,#888);">&times;</button>
      </div>
      <div style="display:flex;gap:0.5rem;margin-top:0.75rem;">
        <select id="advisorProvider" style="flex:1;padding:0.4rem;border:1px solid var(--border,#ddd);border-radius:0.5rem;font-size:0.85rem;background:var(--bg-page,#fff);">
        </select>
        <select id="advisorPreset" style="flex:1;padding:0.4rem;border:1px solid var(--border,#ddd);border-radius:0.5rem;font-size:0.85rem;background:var(--bg-page,#fff);">
          <option value="stock_stance">스탠스 분석</option>
          <option value="stock_add_or_hold">추가매수 vs 유지</option>
        </select>
      </div>
    </div>
    <div style="padding:1rem 1.25rem;max-height:calc(70vh - 200px);overflow-y:auto;" id="advisorResponseArea">
      <div id="advisorResponse"></div>
    </div>
    <div style="padding:0.75rem 1.25rem;border-top:1px solid var(--border,#e5e0db);">
      <div style="display:flex;gap:0.5rem;">
        <textarea id="advisorQuestion" rows="2" placeholder="질문을 입력하세요..."
                  style="flex:1;padding:0.5rem;border:1px solid var(--border,#ddd);border-radius:0.5rem;font-size:0.85rem;resize:none;font-family:inherit;"></textarea>
        <button id="advisorSubmit" onclick="submitAdvisorQuestion()"
                style="padding:0.5rem 1rem;background:var(--accent,#c05a3c);color:#fff;border:none;border-radius:0.5rem;cursor:pointer;font-size:0.85rem;white-space:nowrap;">
          질문하기
        </button>
      </div>
      <div id="advisorLoading" style="display:none;text-align:center;padding:0.5rem;color:var(--text-secondary,#888);font-size:0.85rem;">
        <span class="spinner-border spinner-border-sm"></span> 분석 중...
      </div>
      <small style="display:block;margin-top:0.5rem;color:var(--text-secondary,#999);font-size:0.75rem;">AI 분석 보조 도구이며 투자 자문이 아닙니다.</small>
    </div>
  </div>
```

- [ ] **Step 3: Add JavaScript logic**

Add this inside the last `<script>` block (before the closing `</script>`), after the existing markdown modal code:

```javascript
    // --- AI Advisor ---
    const ADVISOR_SCOPE = "position";
    const ADVISOR_MARKET_TYPE = "{{ page_payload.summary.market_type }}";
    const ADVISOR_SYMBOL = "{{ page_payload.summary.symbol }}";

    let advisorPanelOpen = false;
    let advisorProvidersLoaded = false;

    function toggleAdvisorPanel() {
        advisorPanelOpen = !advisorPanelOpen;
        document.getElementById('advisorPanel').style.display = advisorPanelOpen ? 'block' : 'none';
        if (advisorPanelOpen && !advisorProvidersLoaded) {
            loadAdvisorProviders();
        }
    }

    async function loadAdvisorProviders() {
        try {
            const res = await fetch('/portfolio/api/ai-advice/providers');
            const data = await res.json();
            const select = document.getElementById('advisorProvider');
            select.innerHTML = '';
            if (data.providers.length === 0) {
                select.innerHTML = '<option disabled>API 키를 설정하세요</option>';
                document.getElementById('advisorSubmit').disabled = true;
                document.getElementById('advisorQuestion').disabled = true;
                return;
            }
            data.providers.forEach(p => {
                const opt = document.createElement('option');
                opt.value = p.name;
                opt.textContent = `${p.name} (${p.default_model})`;
                if (p.name === data.default_provider) opt.selected = true;
                select.appendChild(opt);
            });
            advisorProvidersLoaded = true;
        } catch (e) {
            document.getElementById('advisorProvider').innerHTML =
                '<option disabled>로딩 실패</option>';
        }
    }

    async function submitAdvisorQuestion() {
        const question = document.getElementById('advisorQuestion').value.trim();
        if (!question) return;

        const btn = document.getElementById('advisorSubmit');
        const loading = document.getElementById('advisorLoading');
        const responseEl = document.getElementById('advisorResponse');

        btn.disabled = true;
        loading.style.display = 'block';

        try {
            const res = await fetch('/portfolio/api/ai-advice', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    scope: ADVISOR_SCOPE,
                    preset: document.getElementById('advisorPreset').value,
                    provider: document.getElementById('advisorProvider').value,
                    model: null,
                    question: question,
                    market_type: ADVISOR_MARKET_TYPE,
                    symbol: ADVISOR_SYMBOL,
                }),
            });

            if (!res.ok) {
                const err = await res.json();
                responseEl.innerHTML = `<div class="alert alert-warning" style="font-size:0.85rem;">${err.detail || '요청 실패'}</div>`;
                return;
            }

            const data = await res.json();
            if (data.success) {
                responseEl.innerHTML =
                    '<div style="font-size:0.85rem;line-height:1.6;">' +
                    marked.parse(data.answer) +
                    '</div>' +
                    '<small style="color:var(--text-secondary,#999);">' +
                    data.provider + ' / ' + data.model +
                    (data.elapsed_ms ? ' / ' + (data.elapsed_ms / 1000).toFixed(1) + 's' : '') +
                    '</small>';
            } else {
                responseEl.innerHTML = `<div class="alert alert-warning" style="font-size:0.85rem;">${data.error}</div>`;
            }
        } catch (e) {
            responseEl.innerHTML = '<div class="alert alert-warning" style="font-size:0.85rem;">네트워크 오류. 연결을 확인해주세요.</div>';
        } finally {
            btn.disabled = false;
            loading.style.display = 'none';
        }
    }

    // Submit on Ctrl+Enter
    document.getElementById('advisorQuestion').addEventListener('keydown', function(e) {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            submitAdvisorQuestion();
        }
    });
```

- [ ] **Step 4: Verify the template renders**

Run: `uv run python -c "from app.core.templates import templates; print('templates ok')"`
Expected: `templates ok` (no Jinja2 syntax errors).

- [ ] **Step 5: Commit**

```bash
git add app/templates/portfolio_position_detail.html
git commit -m "feat(ai-advisor): add AI advisor panel to position detail page"
```

---

### Task 9: Frontend — Portfolio Dashboard Page

**Files:**
- Modify: `app/templates/portfolio_dashboard.html`

- [ ] **Step 1: Add marked.js CDN**

In `app/templates/portfolio_dashboard.html`, find the Bootstrap Icons `<link>` in the `<head>` and add after it:

```html
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
```

- [ ] **Step 2: Add AI advisor panel HTML**

Insert the same floating button + panel HTML as Task 8 Step 2, but before the last `<script>` tag. The HTML is identical.

- [ ] **Step 3: Add JavaScript logic**

Add the same JS as Task 8 Step 3, except change these three constants at the top:

```javascript
    // --- AI Advisor ---
    const ADVISOR_SCOPE = "portfolio";
    const ADVISOR_MARKET_TYPE = null;
    const ADVISOR_SYMBOL = null;
```

And change the preset `<select>` to only have one option. In the HTML panel, replace the preset select with:

```html
        <select id="advisorPreset" style="flex:1;padding:0.4rem;border:1px solid var(--border,#ddd);border-radius:0.5rem;font-size:0.85rem;background:var(--bg-page,#fff);">
          <option value="portfolio_stance">포트폴리오 스탠스</option>
        </select>
```

- [ ] **Step 4: Commit**

```bash
git add app/templates/portfolio_dashboard.html
git commit -m "feat(ai-advisor): add AI advisor panel to portfolio dashboard"
```

---

### Task 10: Lint + Full Test Suite

**Files:** None (verification only)

- [ ] **Step 1: Run linting**

Run: `make lint`
Expected: No errors. If ruff reports formatting issues, run `make format` and re-check.

- [ ] **Step 2: Run full test suite**

Run: `make test`
Expected: All tests pass, including existing tests and new `test_ai_advisor.py`.

- [ ] **Step 3: Run AI advisor tests specifically**

Run: `uv run pytest tests/test_ai_advisor.py -v`
Expected: All tests PASS with details visible.

- [ ] **Step 4: Fix any issues found**

If lint or tests fail, fix and re-run. Common issues:
- Import ordering (ruff will fix with `make format`)
- Missing type annotations
- Async test missing `@pytest.mark.asyncio`

- [ ] **Step 5: Final commit if fixes were needed**

```bash
git add -u
git commit -m "fix: resolve lint and test issues for AI advisor"
```
