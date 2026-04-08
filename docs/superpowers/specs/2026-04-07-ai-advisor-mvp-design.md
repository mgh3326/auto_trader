# AI Advisor MVP Spec

Portfolio/Position 페이지에서 기존 AI markdown context를 재사용하여 외부 LLM provider에 직접 질문하고 답변을 받는 기능.

## 핵심 원칙

- **기존 AIMarkdownService의 context 생성 로직을 그대로 재사용**
- 기존 `/api/ai-markdown/*` 엔드포인트는 유지 (copy-paste용)
- 새 `/api/ai-advice` 엔드포인트를 추가하여 server-side provider 호출까지 닫음
- indicators/news/opinions를 context에 깊게 합치는 확장은 Phase 2로 미룸
- provider adapter는 얇게 시작

## 기존 코드와 새 코드의 관계

```
기존 (변경 없음)                          신규
──────────────────────                   ──────────────────────
/api/ai-markdown/portfolio  ──┐
/api/ai-markdown/stock      ──┤         /portfolio/api/ai-advice/providers (GET)
                              │         /portfolio/api/ai-advice             (POST)
                              ▼                    │
                     AIMarkdownService ◄───────────┘  (재사용)
                     (context 생성)         AiAdvisorService (신규)
                              │                    │
                     PortfolioOverviewService       ├── OpenAIProvider (GPT+Grok)
                     PositionDetailService          └── GeminiProvider
                     DashboardService
```

**관계 정리:**
- `AIMarkdownService` — 기존 3개 preset(portfolio_stance, stock_stance, stock_add_or_hold)의 markdown 생성 로직 그대로 호출
- `AiAdvisorService` — 새로 만듦. AIMarkdownService로 context 생성 → provider에 전달 → 응답 반환
- 기존 ai-markdown 라우터 — 건드리지 않음. copy-paste 용도로 계속 동작
- 새 ai-advice 엔드포인트 — portfolio 라우터에 추가

## Phase 1 범위 (MVP)

### 만드는 것

| 파일 | 종류 | 설명 |
|------|------|------|
| `app/services/ai_providers/__init__.py` | 신규 | 패키지 |
| `app/services/ai_providers/base.py` | 신규 | `AiProvider` protocol + `AiProviderResult` |
| `app/services/ai_providers/openai_provider.py` | 신규 | GPT + Grok (base_url 분기) |
| `app/services/ai_providers/gemini_provider.py` | 신규 | Gemini adapter |
| `app/services/ai_advisor_service.py` | 신규 | orchestrator: context 생성 + provider dispatch |
| `app/schemas/ai_advisor.py` | 신규 | request/response 스키마 |
| `app/routers/portfolio.py` | 수정 | 2개 엔드포인트 추가 |
| `app/core/config.py` | 수정 | provider API key 설정 추가 |
| `app/templates/portfolio_dashboard.html` | 수정 | AI 질문 패널 추가 |
| `app/templates/portfolio_position_detail.html` | 수정 | AI 질문 패널 추가 |
| `app/main.py` | 변경 없음 | portfolio router가 이미 등록되어 있으므로 변경 불필요 |
| `pyproject.toml` | 수정 | `openai`, `google-genai` 의존성 추가 |
| `env.example` | 수정 | 새 환경변수 문서화 |
| `tests/test_ai_advisor.py` | 신규 | 단위 테스트 |

### 재사용하는 기존 코드

| 기존 코드 | 재사용 방식 |
|-----------|-------------|
| `AIMarkdownService.generate_portfolio_stance_markdown()` | portfolio scope context 생성 |
| `AIMarkdownService.generate_stock_stance_markdown()` | position scope (stance) context 생성 |
| `AIMarkdownService.generate_stock_add_or_hold_markdown()` | position scope (add-or-hold) context 생성 |
| `PortfolioOverviewService.get_overview()` | portfolio scope 데이터 소스 |
| `PortfolioPositionDetailService.get_page_payload()` | position scope 데이터 소스 |
| `PortfolioDashboardService` | journal snapshot 등 보조 데이터 |
| 기존 dependency injection 패턴 | `get_portfolio_overview_service`, `get_position_detail_service` 등 |

### Phase 2/3로 미루는 것

- indicators/news/opinions를 context markdown에 통합
- 질문/응답 히스토리 DB 저장
- SSE streaming 응답
- 사용자별 프롬프트 커스터마이징
- context_summary.missing 상세 표시 (어떤 데이터가 빠졌는지)
- 연속 대화 (multi-turn) 지원

## Config/Env

```python
# app/core/config.py — Settings 클래스에 추가
openai_api_key: str | None = None               # OPENAI_API_KEY
gemini_advisor_api_key: str | None = None        # GEMINI_ADVISOR_API_KEY
grok_api_key: str | None = None                  # GROK_API_KEY
ai_advisor_timeout: float = 60.0                 # AI_ADVISOR_TIMEOUT (초)
ai_advisor_default_provider: str = "gemini"      # AI_ADVISOR_DEFAULT_PROVIDER
```

`GEMINI_ADVISOR_API_KEY`를 기존 `GOOGLE_API_KEYS`와 분리하는 이유: 기존 키는 분석 시스템의 rate-limit 관리 대상이고, advisor는 별도 quota로 운영하는 게 안전.

## Provider Adapter 설계

### Protocol

```python
# app/services/ai_providers/base.py
from pydantic import BaseModel
from typing import Protocol

class AiProviderResult(BaseModel):
    answer: str
    provider: str        # "openai" | "gemini" | "grok"
    model: str           # 실제 사용된 모델명
    usage: dict | None   # {"input_tokens": ..., "output_tokens": ...} 가능하면
    elapsed_ms: int

class AiProviderError(Exception):
    """Provider 호출 에러. 사용자에게 보여줄 메시지와 내부 로그용 원본 분리."""
    def __init__(self, user_message: str, detail: str = ""):
        self.user_message = user_message
        self.detail = detail
        super().__init__(user_message)

class AiProvider(Protocol):
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

### OpenAI Provider (GPT + Grok)

```python
# app/services/ai_providers/openai_provider.py
from openai import AsyncOpenAI

class OpenAIProvider:
    def __init__(
        self,
        api_key: str,
        provider_name: str = "openai",
        default_model: str = "gpt-4o",
        base_url: str | None = None,
    ):
        self.provider_name = provider_name
        self.default_model = default_model
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def ask(self, system_prompt, user_message, model=None, timeout=60.0):
        model = model or self.default_model
        # openai SDK 호출
        # RateLimitError → AiProviderError("요청 한도 초과...")
        # APITimeoutError → AiProviderError("응답 시간 초과...")
        # AuthenticationError → AiProviderError("API 인증 실패...")
```

Grok은 동일 클래스를 `base_url="https://api.x.ai/v1"`, `provider_name="grok"`, `default_model="grok-3-mini"`로 인스턴스화.

### Gemini Provider

```python
# app/services/ai_providers/gemini_provider.py
from google import genai

class GeminiProvider:
    def __init__(self, api_key: str, default_model: str = "gemini-2.5-flash"):
        self.provider_name = "gemini"
        self.default_model = default_model
        self.client = genai.Client(api_key=api_key)

    async def ask(self, system_prompt, user_message, model=None, timeout=60.0):
        model = model or self.default_model
        # genai.Client.aio.models.generate_content() 호출
        # 에러 매핑 동일 패턴
```

## AiAdvisorService

```python
# app/services/ai_advisor_service.py

class AiAdvisorService:
    def __init__(
        self,
        markdown_service: AIMarkdownService,
        overview_service: PortfolioOverviewService,
        detail_service: PortfolioPositionDetailService,
    ):
        self.markdown_service = markdown_service
        self.overview_service = overview_service
        self.detail_service = detail_service
        self.providers: dict[str, AiProvider] = {}
        self._register_configured_providers()

    def _register_configured_providers(self):
        if settings.openai_api_key:
            self.providers["openai"] = OpenAIProvider(api_key=settings.openai_api_key)
        if settings.gemini_advisor_api_key:
            self.providers["gemini"] = GeminiProvider(api_key=settings.gemini_advisor_api_key)
        if settings.grok_api_key:
            self.providers["grok"] = OpenAIProvider(
                api_key=settings.grok_api_key,
                base_url="https://api.x.ai/v1",
                provider_name="grok",
                default_model="grok-3-mini",
            )

    def available_providers(self) -> list[dict]:
        return [
            {"name": name, "default_model": p.default_model}
            for name, p in self.providers.items()
        ]

    async def ask(
        self,
        *,
        user_id: int,
        scope: str,              # "portfolio" | "position"
        preset: PresetType,
        provider: str,
        question: str,
        model: str | None = None,
        # position scope용
        market_type: str | None = None,
        symbol: str | None = None,
        # portfolio scope용
        include_market: str = "ALL",
    ) -> AiAdviceResponse:
        # 1. Context 생성 (기존 AIMarkdownService 재사용)
        context_markdown = await self._generate_context(
            user_id=user_id, scope=scope, preset=preset,
            market_type=market_type, symbol=symbol,
            include_market=include_market,
        )

        # 2. System prompt 조립
        system_prompt = self._build_system_prompt(context_markdown)

        # 3. Provider 호출
        result = await self.providers[provider].ask(
            system_prompt=system_prompt,
            user_message=question,
            model=model,
            timeout=settings.ai_advisor_timeout,
        )

        # 4. 응답 구성
        return AiAdviceResponse(
            success=True,
            answer=result.answer,
            provider=result.provider,
            model=result.model,
            usage=result.usage,
            elapsed_ms=result.elapsed_ms,
        )
```

### Context 생성 흐름

```python
async def _generate_context(self, *, user_id, scope, preset, **kwargs):
    if scope == "portfolio":
        portfolio_data = await self.overview_service.get_overview(
            user_id=user_id, market=kwargs.get("include_market", "ALL"),
        )
        result = self.markdown_service.generate_portfolio_stance_markdown(portfolio_data)
        return result["content"]

    elif scope == "position":
        stock_data = await self.detail_service.get_page_payload(
            user_id=user_id,
            market_type=kwargs["market_type"],
            symbol=kwargs["symbol"],
        )
        if preset == PresetType.STOCK_ADD_OR_HOLD:
            result = self.markdown_service.generate_stock_add_or_hold_markdown(stock_data)
        else:
            result = self.markdown_service.generate_stock_stance_markdown(stock_data)
        return result["content"]
```

**핵심:** AIMarkdownService의 markdown 생성 로직은 그대로 재사용하고, advisor 쪽은 `## 질문` 섹션 분리 등 최소 전처리만 수행.

### System Prompt / User Prompt 경계

```
┌─────────────────────────────────────────────────────┐
│ SYSTEM PROMPT                                        │
│                                                      │
│ [역할 지시] 투자 분석 보조 역할 + 답변 원칙           │
│   - 시나리오 기반 정리                                │
│   - 복수 스탠스 제시                                  │
│   - 조건부 체크리스트 형태                             │
│   - 불확실성 명시                                     │
│                                                      │
│ [컨텍스트] AIMarkdownService가 생성한 markdown 전문   │
│   - 포트폴리오 요약 / 종목 정보 / 매매 계획 등        │
│   - (이미 "## 역할 및 응답 방식" 등이 포함되어 있음)   │
│                                                      │
│ [면책] "투자 자문이 아닌 분석 보조 도구입니다"          │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│ USER PROMPT                                          │
│                                                      │
│ 사용자가 입력한 질문 원문 그대로                       │
│ (예: "비중 조절이 필요한 종목은?")                     │
└─────────────────────────────────────────────────────┘
```

**주의:** 기존 AIMarkdownService markdown에는 이미 `## 질문`과 `## 원하는 답변 형식` 섹션이 포함되어 있음. advisor 호출 시에는 이 부분을 사용자의 실제 질문으로 대체해야 함. 두 가지 방법:

- **방법 A (추천):** markdown의 `## 질문` 이후 부분을 잘라내고, 사용자 질문만 user prompt로 전달. 위의 역할/투자성향/포지션정보 부분은 그대로 system prompt로 사용.
- **방법 B:** markdown 전문을 system prompt에 넣고, 사용자 질문을 user prompt로 별도 전달. `## 질문` 섹션과 user prompt가 중복되지만 LLM이 user prompt를 우선시.

→ **방법 A 채택.** `## 질문` 이전까지를 context로, 사용자 입력을 user message로 깔끔하게 분리.

구현: `_generate_context()`에서 `result["content"]`를 `## 질문` 기준으로 split하여 앞부분만 사용. 이 로직은 fragile할 수 있으므로 전용 helper로 분리하고, marker가 없으면 전체 content를 fallback으로 사용.

```python
def extract_context_before_question(content: str) -> str:
    """'## 질문' 섹션 이전까지만 추출.

    marker가 없으면 content 전체를 그대로 반환 (fallback).
    AIMarkdownService의 출력 형식이 바뀌어도 안전하게 동작.
    """
    marker = "\n## 질문"
    idx = content.find(marker)
    if idx != -1:
        return content[:idx].rstrip()
    return content
```

이 함수는 `ai_advisor_service.py` 내 모듈 레벨 helper로 배치. `_generate_context()`에서 호출.

### System Prompt 래퍼

```python
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
```

## API 설계

### GET /portfolio/api/ai-advice/providers

사용 가능한 provider 목록 반환. UI 초기화 시 1회 호출.

**Response:**
```json
{
  "providers": [
    { "name": "gemini", "default_model": "gemini-2.5-flash" },
    { "name": "openai", "default_model": "gpt-4o" },
    { "name": "grok", "default_model": "grok-3-mini" }
  ],
  "default_provider": "gemini"
}
```

provider가 0개이면 빈 배열. UI에서 "API 키를 설정하세요" 안내 표시.

### POST /portfolio/api/ai-advice

**Request:**
```json
{
  "scope": "portfolio",
  "preset": "portfolio_stance",
  "provider": "gemini",
  "model": null,
  "question": "비중 조절이 필요한 종목은?",
  "include_market": "ALL"
}
```

```json
{
  "scope": "position",
  "preset": "stock_stance",
  "provider": "openai",
  "model": "gpt-4o",
  "question": "추가매수/홀드/축소 각각의 조건을 정리해줘",
  "market_type": "US",
  "symbol": "AAPL"
}
```

**Response (성공):**
```json
{
  "success": true,
  "answer": "## 현재 해석\n...(markdown)...",
  "provider": "gemini",
  "model": "gemini-2.5-flash",
  "usage": { "input_tokens": 2100, "output_tokens": 800 },
  "elapsed_ms": 3200,
  "disclaimer": "AI 분석 보조 도구이며 투자 자문이 아닙니다."
}
```

**Response (실패):**
```json
{
  "success": false,
  "answer": "",
  "provider": "gemini",
  "model": "",
  "error": "요청 한도 초과. 잠시 후 다시 시도해주세요.",
  "elapsed_ms": 150,
  "disclaimer": "AI 분석 보조 도구이며 투자 자문이 아닙니다."
}
```

**필드 규칙:**
- `success=true`일 때: `answer` 필수, `error`는 null
- `success=false`일 때: `answer`는 빈 문자열, `error` 필수
- `disclaimer`, `provider`, `elapsed_ms`는 항상 포함
- `usage`는 provider가 제공하면 포함, 아니면 null
- `include_market`는 `scope=portfolio`일 때만 사용. `scope=position`이면 무시됨

### Scope별 Preset 매핑

| scope | 가능한 preset | 데이터 소스 |
|-------|--------------|-------------|
| `portfolio` | `portfolio_stance` | `PortfolioOverviewService.get_overview()` |
| `position` | `stock_stance` | `PositionDetailService.get_page_payload()` |
| `position` | `stock_add_or_hold` | `PositionDetailService.get_page_payload()` |

## 프론트엔드 UI

### 구조

양쪽 페이지에 동일한 패널을 추가. Bootstrap 5 collapse 또는 offcanvas.

```html
<!-- AI Advisor Panel -->
<div class="ai-advisor-panel">
  <button onclick="toggleAdvisorPanel()">AI 상담</button>

  <div id="advisorPanel" class="collapse">
    <!-- Provider 선택 -->
    <select id="advisorProvider">
      <!-- GET /providers로 동적 채움 -->
    </select>

    <!-- Preset 선택 (position 페이지에서만 2개 선택지) -->
    <select id="advisorPreset">
      <option value="stock_stance">현재 스탠스 분석</option>
      <option value="stock_add_or_hold">추가매수 vs 유지</option>
    </select>

    <!-- 질문 입력 -->
    <textarea id="advisorQuestion" placeholder="질문을 입력하세요..."></textarea>

    <!-- 질문 버튼 -->
    <button id="advisorSubmit" onclick="submitQuestion()">질문하기</button>

    <!-- 로딩 -->
    <div id="advisorLoading" class="d-none">분석 중...</div>

    <!-- 응답 영역 -->
    <div id="advisorResponse"></div>

    <!-- 면책 -->
    <small class="text-muted">AI 분석 보조 도구이며 투자 자문이 아닙니다.</small>
  </div>
</div>
```

### JavaScript 흐름

```javascript
async function initAdvisorPanel() {
    const res = await fetch("/portfolio/api/ai-advice/providers");
    const data = await res.json();
    if (data.providers.length === 0) {
        // "API 키를 설정하세요" 안내 표시, 입력 비활성화
        return;
    }
    // provider 드롭다운 채우기, default 선택
}

async function submitQuestion() {
    const payload = {
        scope: PAGE_SCOPE,          // "portfolio" 또는 "position"
        preset: presetSelect.value,
        provider: providerSelect.value,
        model: null,
        question: questionInput.value,
        // position일 때:
        market_type: MARKET_TYPE,   // template 변수에서
        symbol: SYMBOL,             // template 변수에서
    };

    // 로딩 표시
    submitBtn.disabled = true;
    loadingEl.classList.remove("d-none");
    responseEl.innerHTML = "";

    const res = await fetch("/portfolio/api/ai-advice", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
    const data = await res.json();

    if (data.success) {
        responseEl.innerHTML = renderMarkdown(data.answer);
    } else {
        responseEl.innerHTML = `<div class="alert alert-warning">${data.error}</div>`;
    }

    submitBtn.disabled = false;
    loadingEl.classList.add("d-none");
}
```

markdown → HTML 변환: 기존 프로젝트에 markdown 렌더링 라이브러리가 이미 있으면 그것을 재사용. 없을 경우에만 CDN으로 `marked.js` 추가 (가볍고 기존 CDN 패턴과 일치).

### 에러 처리 원칙

서버 에러와 외부 provider 에러를 구분하여 HTTP status를 다르게 사용:

| 상황 | HTTP Status | 응답 형태 |
|------|-------------|-----------|
| Bad input (잘못된 scope/preset) | **400** | `{ "detail": "..." }` |
| 존재하지 않는 provider 지정 | **400** | `{ "detail": "..." }` |
| 인증 없이 호출 | **401** | `{ "detail": "..." }` |
| Position not found | **404** | `{ "detail": "..." }` |
| 외부 provider rate limit (429) | **200** | `{ "success": false, "error": "요청 한도 초과..." }` |
| 외부 provider timeout | **200** | `{ "success": false, "error": "응답 시간 초과..." }` |
| 외부 provider auth 실패 | **200** | `{ "success": false, "error": "API 인증 실패..." }` |
| 외부 provider 기타 에러 | **200** | `{ "success": false, "error": "AI 응답 생성 실패..." }` |

**원칙:** 클라이언트 잘못(bad input, auth)이나 서버 리소스 부재(position not found)는 4xx. 요청 자체는 올바르지만 외부 provider 호출이 실패한 경우는 200 + `success: false`로 일관 처리. 프론트에서는 `res.ok`이면 `data.success`를 추가 확인하는 2단계 체크.

### Fallback UX

| 상황 | UI 표시 |
|------|---------|
| Provider 0개 (키 미설정) | 패널에 "AI 상담을 사용하려면 API 키를 설정하세요" 안내. 입력 비활성화. |
| 4xx 에러 | 경고 alert: 서버 반환 메시지 표시 |
| `success: false` (provider 실패) | 경고 alert: `error` 필드 메시지 표시 |
| 네트워크 에러 (fetch 실패) | 경고 alert: "네트워크 오류. 연결을 확인해주세요." |

## 구현 순서

### Step 1: Config + Dependencies
- `app/core/config.py` — 5개 설정 추가
- `pyproject.toml` — `openai`, `google-genai` 추가
- `env.example` — 새 환경변수 문서화
- `uv sync`

### Step 2: Provider Adapters
- `app/services/ai_providers/__init__.py`
- `app/services/ai_providers/base.py` — protocol + result + error
- `app/services/ai_providers/openai_provider.py`
- `app/services/ai_providers/gemini_provider.py`

### Step 3: Advisor Service
- `app/schemas/ai_advisor.py` — request/response
- `app/services/ai_advisor_service.py` — orchestrator

### Step 4: API Endpoints
- `app/routers/portfolio.py` — 2개 엔드포인트 추가 + dependency injection

### Step 5: Frontend
- `portfolio_dashboard.html` — AI advisor 패널
- `portfolio_position_detail.html` — AI advisor 패널
- marked.js CDN 추가 (base.html 또는 해당 템플릿)

### Step 6: Tests
- `tests/test_ai_advisor.py`

## 테스트 포인트

### 자동 테스트 (Step 6)
- `AiAdvisorService` — provider mock으로:
  - portfolio scope context 생성 + provider 호출 검증
  - position scope (stance / add-or-hold) context 생성 검증
  - `## 질문` 섹션이 올바르게 잘리는지
  - provider 미설정 시 `available_providers()` 빈 배열
  - provider 에러 시 `AiProviderError` 전파
- OpenAI/Gemini provider — SDK mock으로:
  - 정상 응답 매핑
  - rate limit → `AiProviderError`
  - timeout → `AiProviderError`
  - auth error → `AiProviderError`
- `extract_context_before_question()` helper:
  - `## 질문` marker 있을 때 올바르게 분리
  - marker 없을 때 전체 content fallback
- API endpoint — `TestClient`로:
  - `GET /providers` 응답 형태
  - `POST /ai-advice` 정상 흐름
  - 잘못된 provider / scope → 400
  - 인증 없이 호출 → 401
  - position not found → 404
  - provider 실패 → 200 + `success: false`

### 수동 확인
- `/portfolio/`에서 AI 질문 패널 열기 → provider 선택 → 질문 → 응답 표시
- `/portfolio/positions/{market_type}/{symbol}`에서 동일 흐름
- preset 변경 (stance vs add-or-hold) 시 context 차이 확인
- provider 미설정 상태에서 UI 안내 문구 확인
- 연속 질문 시 과도한 로딩 없이 동작
- timeout 상황 시뮬레이션 (긴 질문 + 짧은 timeout)
- crypto 종목 / 저널 없는 종목에서 graceful 동작
- `make lint && make test` 통과
