# ROB-694 — CLAUDE.md 스테일 문서 정리 (구현 플랜)

> **범위 제약**: 이 작업은 `CLAUDE.md` 단일 파일의 편집만 수행한다. 소스 코드 / 다른 문서 / `docs/archive/*` 원본은 건드리지 않는다. 본 플랜 문서(`docs/plans/ROB-694-...md`) 외에 새 파일을 만들지 않는다.

## 1. 목표

프로젝트 루트 `CLAUDE.md`가 **이미 삭제·이동된 코드를 현행처럼 서술**하는 부분을 제거/수정한다. AI 에이전트가 CLAUDE.md를 ground truth로 읽기 때문에, 스테일 서술은 능동적으로 오도한다(존재하지 않는 `Analyzer`/`UpbitAnalyzer` 상속, 삭제된 Gemini in-process 호출, 410 Gone 대시보드, 30,000줄 `kis.py` 등).

핵심 원칙:
- **REMOVE**: app/ 기준 ref-count 0 이 확인된 삭제 심볼/섹션.
- **REWRITE**: 경로만 이동했고 섹션 자체는 유용 → 경로만 현행화, 살아있는 항목은 보존.
- **KEEP**: 리드가 실제로 살아있으면 증거와 함께 보존(오삭제 방지).

---

## 2. 섹션별 검증표

각 대상: 헤딩/앵커 → 판정 → grep 근거(심볼 → app/ ref count) → 정확한 편집 지시.

### 검증에 쓴 사실 (공통 근거)

| 리드 | 결과 |
|---|---|
| `app/analysis/analyzer.py` | **삭제됨**. `git log --oneline -- app/analysis/analyzer.py` → `1d1f7976 refactor: remove Gemini API dependency from analysis module (#423)` |
| `app/analysis/service_analyzers.py` | **삭제됨**. 동일 커밋 `1d1f7976` (#423) |
| flat `app/services/{kis,yahoo,upbit}.py`, `app/tasks/kis.py` | **모두 삭제됨** (파일 부재 확인). 클라이언트는 `app/services/brokers/{kis,yahoo,upbit}/` 로 이동 (`53b538ac refactor: remove provider shims and migrate direct broker imports`). `app.tasks` → `app.jobs` 마이그레이션(`11ea7e6b`) |
| ROB-501 in-process LLM 가드 | `tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py` **존재** (app/ 전체 forbidden provider import 스캔) |
| `/analysis-json`, `/stock-latest` | **410 Gone**. `app/routers/deprecated_pages.py` `LEGACY_PREFIXES` 에 `/stock-latest`(L16), `/analysis-json`(L17) 포함 → `_register_prefix` 로 410 응답. 템플릿 파일 없음(핸들러가 생성 HTML 반환) |

---

### 2.1 `### 분석 시스템 아키텍처` (CLAUDE.md L92–L111) → **REMOVE**

**grep 근거 (app/ 한정):**
- `class Analyzer` → **0**
- `class DataProcessor` / `DataProcessor` → **0** / **0**
- `class UpbitAnalyzer` / `UpbitAnalyzer` → **0** / **0**
- `class YahooAnalyzer` / `YahooAnalyzer` → **0** / **0**
- `class KISAnalyzer` / `KISAnalyzer` → **0** / **0**
- `service_analyzers` → **0**, `analysis.analyzer` → **0**
- (거짓 양성 주의) `git grep "Analyzer" app/` = 20건이지만 **전부** 신규 스테이지 파이프라인 클래스: `BaseStageAnalyzer`/`FundamentalsStageAnalyzer`/`MarketStageAnalyzer`/`NewsStageAnalyzer`/`SocialStageAnalyzer` (`app/analysis/stages/`, `app/analysis/pipeline.py`). 삭제된 `Analyzer` 계열과 무관.
- 확증: `app/jobs/analyze.py:73` = `# Analyzer removed - return placeholder response`

**편집 지시:** 헤딩 `### 분석 시스템 아키텍처`(L92)부터 마지막 불릿 `- 분봉 데이터가 있으면 \`minute_candles\`로 전달`(L111)까지 **subsection 전체를 삭제**(L92–L111, 앞뒤 공백줄 1개로 정리). 상위 `## 아키텍처`(L90) H2와 바로 다음 `### Runtime LLM ownership boundary`(L113)는 **그대로 유지**. (신규 stages 파이프라인 문서화는 본 이슈 범위 밖 — 추가하지 않음.)

---

### 2.2 `### 1. 새로운 서비스 분석기 추가` (CLAUDE.md L556–L596) → **REMOVE**

**grep 근거 (app/):** `NewServiceAnalyzer` → **0**, `analyze_and_save` → **0**, `merge_historical_and_current` → **0**. 예시 코드가 삭제된 `class NewServiceAnalyzer(Analyzer)` 상속·`DataProcessor.merge_historical_and_current`·`analyze_and_save` 를 현행처럼 서술.

**편집 지시:** 헤딩 `### 1. 새로운 서비스 분석기 추가`(L556)부터 코드펜스 닫힘(L596)까지 삭제. **부수 편집(renumber)**: `## 주요 워크플로우` 하위에서 이 삭제와 §2.4 삭제 이후 유효하게 남는 것은 `### 2. 데이터베이스 모델 변경`(L598) 하나 → 이를 `### 1. 데이터베이스 모델 변경` 으로 **번호 재조정**.

---

### 2.3 `### 3. JSON 분석 결과 사용` (CLAUDE.md L615–L634) → **REMOVE**

**grep 근거 (app/):** `analyze_coins_json` → **0**, `service_analyzers` → **0**, `UpbitAnalyzer` → **0**. 예시가 `from app.analysis.service_analyzers import UpbitAnalyzer` + `analyzer.analyze_coins_json(...)` (삭제 경로/심볼).
- 참고: 코드 주석의 `StockAnalysisResult`/`PromptResult` **테이블 자체는 살아있음**(`app/models/analysis.py`, `app/models/prompt.py`) — DB 정규화 섹션(§2.9 KEEP)에서 보존. 여기서는 삭제 심볼(`UpbitAnalyzer`/`analyze_coins_json`)을 쓰는 **워크플로 예시**만 제거.

**편집 지시:** 헤딩 `### 3. JSON 분석 결과 사용`(L615)부터 코드펜스 닫힘(L634)까지 삭제.

---

### 2.4 프로젝트 개요 Gemini/모델제한 + `### 4. Redis 모델 제한 관리` → **REMOVE / REWRITE**

**grep 근거 (app/):**
- `model_rate_limit` → **0** (CLAUDE.md·blog images·docs/archive·docs/plans 에만 잔존; 런타임 코드 0).
- `gemini` (대소문자 무시) → 11건이나 **전부 비-in-process**: `app/jobs/analyze.py`·`app/jobs/kis_trading.py` = `"Gemini analyzer removed. Agent-based analysis coming soon."` 플레이스홀더 메시지; `app/models/news.py:226` = docstring 예시(`e.g., gemini-2.5-pro` 모델명 문자열); `app/services/action_report/snapshot_backed/request.py:77` = **out-of-process** MCP 컴포저 언급 주석. → in-process Gemini API 호출 **없음**.
- ROB-501 가드 존재(위 표) — in-process LLM 재도입 금지가 코드로 강제됨.

**편집 지시 (4곳):**
1. **L7** `... Google Gemini AI를 활용하여 투자 분석을 제공합니다.` → Gemini 특정 표현 제거로 리라이트. 예: `... 금융 데이터를 수집하고, out-of-process AI 에이전트(MCP consumer / Hermes)를 통해 투자 분석을 제공합니다.` (in-process provider 미언급.)
2. **L12** 불릿 `- AI 분석: Google Gemini API를 통한 구조화된 JSON 분석` → **제거**하거나 `- AI 분석: out-of-process MCP/Hermes 에이전트가 담당 (런타임은 in-process LLM provider 미탑재 — ROB-501 가드)` 로 리라이트. 권장: 리라이트(개요 유지).
3. **L13** 불릿 `- Redis 기반 API 키별 모델 제한 시스템` → **제거** (`model_rate_limit` 0-ref). ⚠️ Redis 자체는 유지(토큰 매니저·캐시·single-flight) — 다른 Redis 서술(docker/env/문제해결)은 **건드리지 않음**.
4. **L636–L647** `### 4. Redis 모델 제한 관리` subsection 전체(`model_rate_limit:*` redis-cli 예시 포함) **삭제**.

---

### 2.5 `### JSON 분석 대시보드` (`/analysis-json`, L724–L727) → **REMOVE**
### 2.6 `### 최신 종목 정보 대시보드` (`/stock-latest`, L729–L731) → **REMOVE**

**grep/근거:** `app/routers/deprecated_pages.py` `LEGACY_PREFIXES` 에 `"/stock-latest"`, `"/analysis-json"` 포함 → 모든 메서드 410 Gone (`_register_prefix`). 별도 템플릿 파일 없음. 두 URL은 현행 접속 시 410.

**편집 지시:** L724–L731(두 subsection) 삭제. `### Trading Policy YAML 단일 소스 (ROB-646)`(L733)는 현행이므로 **유지**.
- **선택(권장, 최소)**: 삭제 후 `## 웹 대시보드`(L722) H2 아래엔 Trading Policy YAML 만 남아 헤딩 의미가 다소 어긋남. 최소 편집으론 그대로 두어도 무방. 원하면 한 줄 대체 노트 추가 가능: `- 현행 웹 대시보드는 \`/invest/\` (구 \`/analysis-json\`·\`/stock-latest\` 는 410 Gone).` — 범위상 optional, 미적용해도 완료기준 충족.

---

### 2.7 `### API 서비스 클라이언트` (L451–L465) → **REWRITE**

**근거:** 트리(L453–L460)가 flat `app/services/upbit.py`·`yahoo.py`·`kis.py (30,000+ 라인)` 를 나열 — **셋 다 삭제**. 클라이언트는 `app/services/brokers/{upbit,yahoo,kis}/` 로 이동(`53b538ac`). 확인: `brokers/upbit/{client,orders,public_trades}.py`, `brokers/yahoo/client.py`, `brokers/kis/{client,account,domestic_orders,overseas_orders,market_data,...}.py`. `brokers/kis/*.py` 합계 ≈ 6,123줄(단일 30k 파일 아님; `client.py` 등으로 분할). **유지되는 것**: `app/services/upbit_websocket.py`(존재), `app/services/redis_token_manager.py`(존재).

**편집 지시:**
- L453–L460 코드블록을 아래처럼 리라이트:
  ```
  app/services/brokers/
  ├── upbit/       # Upbit API (암호화폐) — client.py, orders.py, public_trades.py
  ├── yahoo/       # Yahoo Finance API — client.py
  ├── kis/         # 한국투자증권 API — client.py, account.py, domestic/overseas_orders.py, market_data 등 (파일 분할)
  └── toss/ · kiwoom/ · alpaca/ · binance/   # 기타 브로커
  app/services/
  ├── upbit_websocket.py       # Upbit 실시간 시세
  └── redis_token_manager.py   # Redis 기반 토큰 관리
  ```
- **주의사항 불릿(L462–L465)**: L463 `- \`kis.py\`는 매우 큰 파일(30,000+ 라인)이므로 읽을 때 offset/limit 사용` → **제거**(단일 kis.py 없음; brokers/kis/ 분할). L464(KIS 분봉 `time_unit` 이슈)·L465(Upbit WebSocket+REST) 두 불릿은 **유지**.

---

### 2.8 `### 해외주식 심볼 변환 시스템` — 적용된 파일 (L410–L449) → **REWRITE (경로만)**

**근거 (파일 존재/심볼 사용 확인):**
- `app/core/symbol.py` **존재**, `to_kis_symbol`(L10)/`to_yahoo_symbol`(L18)/`to_db_symbol`(L26) **살아있음** → **구조 블록(L417–L423) KEEP**.
- `to_kis_symbol|to_yahoo_symbol|to_db_symbol` 호출부 30+ 파일(브로커/jobs/mcp_server/services) — 유틸 자체는 광범위 사용.

**적용된 파일 리스트(L425–L430) 판정:**
| L | 항목 | 판정 | 근거 |
|---|---|---|---|
| 426 | `app/services/kis.py` | REWRITE | 삭제 → `app/services/brokers/kis/`(overseas_market_data.py·overseas_orders.py 가 `to_kis_symbol` 사용) |
| 427 | `app/services/yahoo.py` | REWRITE | 삭제 → `app/services/brokers/yahoo/client.py`(`to_yahoo_symbol` 사용 확인) |
| 428 | `app/tasks/kis.py` | REWRITE | 삭제(`app.tasks`→`app.jobs`, `11ea7e6b`) → `app/jobs/`(kis_market_adapters.py·kis_mock_reconciliation_job.py 가 심볼 정규화 사용) |
| 429 | `app/services/kis_holdings_service.py` | **KEEP** | 존재; `from app.core.symbol import to_db_symbol`(L8), 사용 L19/L36 |
| 430 | `app/services/kis_trading_service.py` | **KEEP** | 존재; `to_db_symbol` import(L20), 사용 L139/L144 |

- 테스트 `tests/test_symbol_conversion.py` **존재** → L446–L449 **KEEP**.
- `scripts/migrate_symbols_to_dot_format.sql` **존재** → L440–L444 **KEEP**.
- DB 테이블 표(L432–L438) — DB 스키마 서술, **KEEP**.

**편집 지시:** L426→`app/services/brokers/kis/`, L427→`app/services/brokers/yahoo/client.py`, L428→`app/jobs/` (또는 비-망라 목록임을 감안해 "주요 브로커/job 호출부에 배선"으로 축약) 로 경로만 교체. L429·L430 및 나머지(구조 블록/테이블/마이그레이션/테스트)는 **그대로**.

---

### 2.9 `### 데이터베이스 정규화 구조` (L384–L408) → **KEEP** (오삭제 방지)

**근거:** `StockAnalysisResult`/`stock_analysis_results` **LIVE** (`app/models/analysis.py`). `StockInfo` **LIVE** (동). `create_stock_if_not_exists` **LIVE** (`app/services/stock_info_service.py:135`, 호출: `app/analysis/pipeline.py`·`app/routers/agent_callback.py`·`app/services/research_pipeline_service.py`). → **편집 없음**.

---

### 2.10 `### 데이터 구조` KR/US 심볼 유니버스 (L467–L490) → **KEEP**

**근거:** `kr_symbol_universe_service.py`·`upbit_symbol_universe_service.py`·`us_symbol_universe_service.py` **모두 존재**; sync 스크립트도 존재. → **편집 없음**.

---

### 2.11 `## 참고 문서` 아카이브 링크 (L772–L773) → **REWRITE (아카이브 강등/주석)**

**근거:** `docs/archive/JSON_ANALYSIS_README.md`·`docs/archive/ANALYSIS_REFACTOR_README.md` **파일 존재하나** 삭제된 Gemini analyzer 시대 코드를 서술(`ANALYSIS_REFACTOR_README.md` 에 `model_rate_limit` 잔존). 파일 자체는 히스토리로 보존하되 **현행 가이드로 오인 금지**.

**편집 지시:** 두 링크에 아카이브/과거 표기 부기:
- L772 → `- \`docs/archive/JSON_ANALYSIS_README.md\` — (아카이브·과거) 삭제된 Gemini analyzer 시절 JSON 분석 문서, 현행 아님`
- L773 → `- \`docs/archive/ANALYSIS_REFACTOR_README.md\` — (아카이브·과거) 삭제된 analyzer/Redis 모델제한 시절 문서, 현행 아님`

나머지 4개 링크는 **KEEP** (실체 확인): `STOCK_INFO_GUIDE.md`(존재; DB 정규화 — 유효), `UPBIT_WEBSOCKET_README.md`(존재; `app/services/upbit_websocket.py` 유효), `DEPLOYMENT.md`(존재), `DOCKER_USAGE.md`(존재).

---

### 2.12 부수 KEEP (건드리지 말 것)

- `### Runtime LLM ownership boundary`(L113–) — 현행, 가드 테스트 인용. **유지**.
- Redis 인프라 서술: docker(L43–), env `REDIS_URL/REDIS_HOST...`(L666–), `### Redis 연결 실패`(L750–) — Redis는 토큰/캐시로 여전히 사용. **유지** (제거 대상은 `model_rate_limit` 뿐).
- `### KIS 분봉 API 문제`(L745–) — KIS `time_unit` 이슈 현행. **유지**.

---

## 3. 최종 완료기준 (검증 커맨드)

편집 후, CLAUDE.md 내 **삭제-심볼 잔존 0** 을 아래로 확인한다 (worktree 루트에서):

```bash
cd /Users/mgh3326/work/auto_trader.rob-694

# (A) 삭제된 analyzer/워크플로 심볼이 CLAUDE.md 에 남아있지 않아야 함 → 각 0
git grep -nE 'analyzer\.py|service_analyzers|UpbitAnalyzer|YahooAnalyzer|KISAnalyzer|DataProcessor|NewServiceAnalyzer|analyze_and_save|merge_historical_and_current|analyze_coins_json|model_rate_limit' -- CLAUDE.md
#   기대: 출력 없음(0)

# (B) 삭제된 flat 경로가 CLAUDE.md 에 남아있지 않아야 함 → 각 0
git grep -nE 'app/services/kis\.py|app/services/yahoo\.py|app/services/upbit\.py|app/tasks/kis\.py|30,000\+ 라인' -- CLAUDE.md
#   기대: 출력 없음(0)

# (C) Gemini in-process 서술 제거 확인 (Runtime LLM ownership boundary 의 "Gemini/OpenAI/Grok" 금지 문구는 정상 → 그 라인 제외 후 0)
git grep -n 'Google Gemini' -- CLAUDE.md
#   기대: 출력 없음(0)

# (D) 410 대시보드 URL 이 "현행 대시보드"로 서술되지 않음
git grep -nE '/analysis-json|/stock-latest' -- CLAUDE.md
#   기대: 출력 없음(0)  (선택 노트로 "410 Gone" 맥락 언급 시엔 그 라인만 허용)
```

추가 정합성:
- 편집 후 CLAUDE.md 헤딩 구조 유효(H2/H3 계층 깨짐 없음), 워크플로 번호 재조정(§2.2) 반영.
- 소스 코드/`docs/archive` 원본 무변경 (`git status` 에 `CLAUDE.md` + 본 플랜 파일만).

---

## 4. 리스크 (거의 0)

- **런타임 영향 없음**: 문서 전용 편집. 코드/테스트/CI 무관.
- **오삭제 리스크 완화**: 살아있는 항목(`app/core/symbol.py`·`kis_holdings_service`·`kis_trading_service`·DB 정규화·심볼 유니버스·Redis 인프라·`StockAnalysisResult`)은 grep 증거로 KEEP 명시.
- **유일한 판단 포인트**: (a) 워크플로 번호 재조정(§2.2), (b) `## 웹 대시보드` H2 잔존 형태(§2.6 선택) — 둘 다 저위험, 완료기준에 영향 없음.
- **아카이브 파일**: 삭제하지 않고 주석 강등만 → 히스토리 보존 + 오도 방지 양립.

## 편집 규모 요약

- **REMOVE**: 6개 섹션 — §2.1 분석 시스템 아키텍처, §2.2 워크플로#1, §2.3 워크플로#3, §2.4 개요 불릿 2개 + 워크플로#4(Redis 모델제한), §2.5 JSON 대시보드, §2.6 최신종목 대시보드.
- **REWRITE**: 3개 섹션 — §2.4 개요 L7 리라이트(Gemini 제거), §2.7 API 서비스 클라이언트 트리+주의사항, §2.8 심볼변환 적용파일 경로 3건, §2.11 참고문서 아카이브 강등 2건.
- **KEEP(예상외 아님, 증거로 확정)**: 데이터베이스 정규화 구조, KR/US 심볼 유니버스, app/core/symbol.py 구조블록/테이블/마이그레이션/테스트, Redis 인프라, Runtime LLM ownership boundary, StockAnalysisResult 테이블.
