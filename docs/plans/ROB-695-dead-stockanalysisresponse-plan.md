# ROB-695 — 죽은 `StockAnalysisResponse` 스키마 제거 (PriceAnalysis/PriceRange 보존) 구현 플랜

Branch: `chore/ROB-695-dead-stockanalysisresponse`
Worktree: `/Users/mgh3326/work/auto_trader.rob-695`
Base: `origin/main` @ `b2e12790` (worktree HEAD)
성격: 순수 삭제(dead-code removal). 소스 마이그레이션 0, 런타임 동작 변화 0.

---

## 1. 목표

죽은 pydantic 스키마 `StockAnalysisResponse` 정의와 그 패키지 재수출, 그리고
그것만을 소비하는 미호출 테스트 헬퍼 `build_stock_analysis_response`를 제거한다.

**절대 보존:** 같은 파일(`app/analysis/models.py`)의 `PriceAnalysis` / `PriceRange`.
둘 다 라우터·debate 파이프라인에서 **live** 사용 중이므로 손대지 않는다.

이것은 one-way door(삭제)이므로 아래 §2에서 deadness를 file:line + grep ref-count로
엄밀히 증명한 뒤에만 §3 제거를 수행한다.

---

## 2. 검증된 현재 상태 (worktree 실측)

### 2.1 `StockAnalysisResponse` 정의 및 참조 (`git grep -n StockAnalysisResponse`)

| # | 위치 (file:line) | 종류 | 처리 |
|---|---|---|---|
| 1 | `app/analysis/models.py:28` | **정의** (class body 28–39) | **삭제** |
| 2 | `app/analysis/__init__.py:2` | 패키지 import (재수출용) | **수정**(심볼만 제거) |
| 3 | `app/analysis/__init__.py:8` | `__all__` 항목 | **삭제**(1줄) |
| 4 | `tests/_analysis_support.py:5` | test import | **삭제**(줄 전체, §3.3 참고) |
| 5 | `tests/_analysis_support.py:70` | 헬퍼 def 반환타입 | **삭제**(함수 70–86) |
| 6 | `tests/_analysis_support.py:71` | 헬퍼 내부 생성자 호출 | **삭제**(위 함수에 포함) |
| 7 | `app/analysis/AGENTS.md:9` | KB 표의 문서 언급 | **수정**(stale 참조 정리, §3.4) |
| 8 | `docs/archive/JSON_ANALYSIS_README.md:224,226,275` | 아카이브 문서 | **보존**(아카이브, 이력 문서 — 건드리지 않음) |

**런타임(`app/**`) 참조 = 0.** 정의(#1)와 재수출(#2,#3)을 제외하면 `app/` 어디에서도
`StockAnalysisResponse`를 import/사용하지 않는다. (§2.4에서 교차 확인)

### 2.2 `build_stock_analysis_response` 헬퍼 호출자 (`git grep -n build_stock_analysis_response`)

| 위치 | 종류 |
|---|---|
| `tests/_analysis_support.py:70` | **정의뿐** |

**호출자 = 0.** repo 전체에서 이 함수를 호출/import하는 곳이 없다.

### 2.3 `_analysis_support` 모듈 자체의 importer (전 repo grep)

- `from ... _analysis_support import` / `import _analysis_support` 형태 매치 = **0건**.
- `git grep -n analysis_support`가 잡는 `tests/test_agent_gateway.py:151`,
  `.test_durations`의 `test_request_analysis_supports_screener_callback_schema`는
  **함수명 부분문자열 오탐**(`analysis_support`s…)이며 모듈 import가 아니다.
- 즉 `tests/_analysis_support.py`는 PR #284(`78d77c7d`) 생성 이후 **어떤 테스트도
  import하지 않는 완전 고아 모듈**이다. (§6 관찰/스코프 참고 — ROB-695는 이 중
  `build_stock_analysis_response`만 대상)

### 2.4 보존 대상 `PriceAnalysis` / `PriceRange` live 사용처 (grep 재확인)

`PriceAnalysis` — **live**:
- `app/analysis/models.py:11`(정의), `:14/17/20/23/37`(자체 필드) — 보존
- `app/routers/screener.py:9`(import), `:59`(응답 필드 타입) — **런타임 live**
- `app/routers/agent_callback.py:8`(import), `:70`(응답 필드 타입) — **런타임 live**
- `app/analysis/debate.py:5`(import), `:126`(`price_analysis=PriceAnalysis()`) — **런타임 live**
- `tests/services/test_legacy_stock_analysis_adapter.py:10,58` — 테스트 live
- `tests/_analysis_support.py:78` — 삭제될 헬퍼 내부(§3.3에서 함께 제거)

`PriceRange` — **live**:
- `app/analysis/models.py:4`(정의), `:14/17/20/23`(`PriceAnalysis` 필드 타입) — 보존
- `tests/_analysis_support.py:79–82` — 삭제될 헬퍼 내부(§3.3에서 함께 제거)
- (주의: `WatchPriceRange`(`app/schemas/investment_reports.py`)는 **다른 심볼** — 무관)

→ `PriceAnalysis`/`PriceRange`는 라우터 2곳 + debate 1곳에서 실사용되므로 **보존 필수.**

### 2.5 이름 충돌 없음 확인
- `StockAnalysisResult`(DB ORM, `app/models/analysis.py`, market_brief_tools 등에서 live)는
  이름이 비슷할 뿐 **완전히 다른 심볼**. ROB-695 제거 대상 아님, 건드리지 않음.
- `PriceAnalysis`(`app/schemas/research_pipeline.py:97`)도 **별개 스키마** — 무관.

---

## 3. 정확한 제거 목록 (파일별 심볼/라인/블록)

### 3.1 `app/analysis/models.py` — `StockAnalysisResponse` 클래스 삭제
- 삭제: **28–39행** (`class StockAnalysisResponse(BaseModel): ... confidence` 전체)
  및 그 앞의 빈 줄 처리(파일 끝이 되므로 27행 아래 잉여 공백 없이 `PriceAnalysis`가 마지막
  클래스가 되도록 정리).
- **보존: 1–25행** (`from pydantic import BaseModel, Field`, `PriceRange`, `PriceAnalysis` 전부).
- `BaseModel`/`Field` import는 `PriceRange`/`PriceAnalysis`가 계속 사용 → 그대로 둔다.

결과 파일 = `PriceRange` + `PriceAnalysis`만 남음.

### 3.2 `app/analysis/__init__.py` — 재수출 제거
- 2행: `from .models import PriceAnalysis, PriceRange, StockAnalysisResponse`
  → `from .models import PriceAnalysis, PriceRange`
- 8행: `    "StockAnalysisResponse",` **줄 삭제**
- 보존: `add_indicators`, `"PriceRange"`, `"PriceAnalysis"` (5–7행) 그대로.

결과 `__all__` = `["add_indicators", "PriceRange", "PriceAnalysis"]`.

### 3.3 `tests/_analysis_support.py` — 헬퍼 + 이제 죽은 import 삭제
- **70–86행** `build_stock_analysis_response()` 함수 전체 삭제(앞 빈 줄 포함 정리).
- **5행** `from app.analysis.models import PriceAnalysis, PriceRange, StockAnalysisResponse`
  **줄 전체 삭제.**
  - 근거: 이 모듈 안에서 `PriceAnalysis`/`PriceRange`/`StockAnalysisResponse`는 오직
    `build_stock_analysis_response`(78–82행) 내부에서만 쓰인다. 함수 제거 후 셋 다 미사용이
    되므로 import를 남기면 **ruff F401(unused import)로 lint 실패**한다. 반드시 함께 제거.
- 보존: `build_analysis_sample_df`, `sample_fundamental_info`, `sample_position_info`,
  `build_minute_candles`(8–67행)와 `pandas`/`__future__` import(1–3행)는 이번 스코프 밖 —
  그대로 둔다. (§6 관찰 참고)

### 3.4 `app/analysis/AGENTS.md` — stale 문서 참조 정리 (문서, 코드 아님)
- 9행 표 셀 `Structured analysis schema (StockAnalysisResponse, ranges)` 를
  삭제된 심볼을 가리키지 않도록 갱신(예: `Structured analysis schema (PriceAnalysis / PriceRange)`).
- 순수 문서 변경으로 런타임 영향 0. (선택적이지만 dead 참조 잔존 방지를 위해 포함 권장.)

### 3.5 손대지 않는 것 (명시적 제외)
- `docs/archive/JSON_ANALYSIS_README.md` — 아카이브 이력 문서. 보존.
- `app/analysis/models.py`의 `PriceRange`/`PriceAnalysis` — 보존(§4).
- 라우터/`debate.py`/DB 모델/마이그레이션 — 무변경.

---

## 4. 보존 경계 (무손상 계약)

| 심볼 | 파일 | 무손상 근거 |
|---|---|---|
| `PriceRange` | `app/analysis/models.py:4–8` | `PriceAnalysis` 4개 필드 타입 + `__all__` 재수출. 삭제 블록(28–39)과 물리적으로 분리. |
| `PriceAnalysis` | `app/analysis/models.py:11–25` | screener.py·agent_callback.py 응답 스키마 + debate.py 생성자에서 **런타임 live**. 삭제 블록과 분리. |
| `__all__`의 `PriceRange`/`PriceAnalysis` | `app/analysis/__init__.py:6–7` | 그대로 유지. `StockAnalysisResponse` 항목만 8행에서 제거. |
| `add_indicators` 재수출 | `app/analysis/__init__.py:1,5` | 무관, 유지. |

제거 경계는 **오직** (a) `models.py` 28–39행, (b) `__init__.py` 2행 심볼목록·8행,
(c) `_analysis_support.py` 5행·70–86행, (d) `AGENTS.md` 9행 문서 셀. 그 외 라인 불변.

---

## 5. 테스트 계획 (제거 후 green 확인)

삭제 대상이 어떤 프로덕션/테스트 코드에도 참조되지 않으므로 신규 테스트는 불필요.
아래로 회귀 없음을 증명한다.

1. **참조 0 재확인 (제거 전/후 각각):**
   - `git grep -n StockAnalysisResponse` → 제거 후 기대: `docs/archive/JSON_ANALYSIS_README.md`
     항목만 남고(아카이브, 의도적 보존) `app/`·`tests/`·`AGENTS.md`에는 **0건**.
   - `git grep -n build_stock_analysis_response` → 제거 후 **0건**.
2. **Import 스모크:** `uv run python -c "import app.analysis; from app.analysis import PriceAnalysis, PriceRange"`
   → 에러 없이 통과(재수출 무손상 확인).
3. **보존 심볼 소비처 스위트 green:**
   - `uv run pytest tests/services/test_legacy_stock_analysis_adapter.py -v`
     (`PriceAnalysis`/`PriceRange` 직접 소비)
   - `uv run pytest tests/test_agent_gateway.py -q`
     (screener/agent_callback 스키마 인접 경로; `PriceAnalysis` 응답 타입 회귀 확인)
   - debate 파이프라인 관련 스위트가 있으면 함께: `uv run pytest -q -k "debate or screener or analysis"`
4. **Lint/type green:** `make lint` (ruff: `_analysis_support.py`의 unused-import를 §3.3에서
   제거했으므로 F401 없음 확인) + `make typecheck`.
5. **전체 스위트(권장):** `make test` — 삭제가 다른 스위트 수집을 깨지 않음을 확인.

기대: 제거로 인해 **깨지는 기존 테스트 0건**. (`build_stock_analysis_response` 호출자 0,
`_analysis_support` 모듈 importer 0이므로 어떤 테스트 수집도 영향받지 않음.)

---

## 6. 리스크 및 근거

- **런타임 리스크: 없음.** `app/**`에서 `StockAnalysisResponse`를 import/사용하는 곳 0건
  (§2.1). 정의와 패키지 재수출만 존재. 라우터/서비스/MCP/jobs 어디도 소비 안 함.
- **DB/마이그레이션 리스크: 없음.** `StockAnalysisResponse`는 순수 pydantic 스키마이지
  ORM 모델이 아니다. DB 매핑 없음, alembic 무변경. 유사 이름 `StockAnalysisResult`(ORM)는
  별개이며 미변경(§2.5).
- **라우트/응답계약 리스크: 없음.** 실제 응답 스키마는 `PriceAnalysis`이며 보존된다.
  screener.py·agent_callback.py의 필드 타입 `PriceAnalysis`는 그대로.
- **Lint 회귀 리스크:** `_analysis_support.py`에서 함수만 지우고 5행 import를 남기면
  ruff F401 실패 → §3.3에서 import 줄을 함께 제거하여 방지(필수).
- **오삭제 리스크(보존물):** `PriceAnalysis`/`PriceRange`는 삭제 블록과 물리적으로 분리된
  1–25행이며 §4에서 무손상 경계를 못박음. 삭제는 28–39행 이후로 한정.
- **관찰/스코프 밖(non-blocking):** `tests/_analysis_support.py`의 나머지 4개 헬퍼
  (`build_analysis_sample_df`, `sample_fundamental_info`, `sample_position_info`,
  `build_minute_candles`)도 현재 **호출자 0**이라 이번 변경 후 파일 전체가 dead가 된다.
  ROB-695 스코프는 `StockAnalysisResponse` + `build_stock_analysis_response`에 한정되므로
  나머지 헬퍼/파일 전체 삭제는 이 PR에 **포함하지 않는다**(별도 후속 정리 후보로만 기록).
  원하면 리뷰어 승인 하에 파일 전체 삭제로 확장 가능하나, 최소 경계 원칙상 기본은 스코프 유지.

---

## 7. 실행 순서 요약
1. `app/analysis/models.py` 28–39행 삭제.
2. `app/analysis/__init__.py` 2행 심볼 제거 + 8행 삭제.
3. `tests/_analysis_support.py` 70–86행(함수) + 5행(import) 삭제.
4. `app/analysis/AGENTS.md` 9행 문서 셀 갱신.
5. §5의 grep/lint/typecheck/pytest로 green 확인.
6. 커밋 → PR(base `main`). 마이그레이션/배포 게이트 없음.
