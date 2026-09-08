# MCP·분석 계약

이 파일은 작업 분야와 무관하게 작업 시작 전에 끝까지 읽어야 하는 필독 계약의 일부다. 찾아보기는 탐색 보조일 뿐 선택 읽기 면제가 아니다.

## 목차

- MCP 표면 계약
- Runtime LLM ownership boundary
- Investment Report Item Contract
- Hermes Report Generation (ROB-287)
- investment_report_create item 계약 (ROB-458)
- get_news 관련성 파이프라인 (ROB-491)

## 기준 원문 계약

### MCP 표면 계약

레인 allowlist 정본: `config/mcp_lane_allowlists/` (`tool<TAB>basis` 보존).
계약 테스트: `tests/mcp_server/test_lane_allowlist_contract.py` — 레인별 배정 프로필의 실제 등록 합집합 검증.
등록 스냅샷: `tests/mcp_server/test_profile_tool_snapshot.py`; D 제거·C niche 관측: `docs/runbooks/mcp-surface-cleanup-20260905.md`.

### Runtime LLM ownership boundary

auto_trader runtime code must not import or instantiate in-process LLM providers
(Gemini/OpenAI/Grok/etc.). LLM judgment belongs to out-of-process MCP consumers
(claude sessions, etc.). The static guard in
`tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py`
scans `app/**/*.py` for forbidden provider imports and deleted provider files.

### Investment Report Item Contract

`investment_report_create` / `investment_report_add_items` reject unknown top-level item keys. Use typed fields for current contracts:

- `trigger_checklist`: `string[]`; copied to watch trigger notifications.
- `max_action`: structured execution-plan JSON for watch items. `account_mode` is required when `max_action` is present; it also requires `side` and exactly one of `quantity` or `notional`; optional keys include `amount_krw`, `limit_price`, `limit_price_hint`, and `ladder_level`.
- Do not send `planned_action` as an item key. Hermes payloads derive `planned_action` from `max_action`.

### Hermes Report Generation (ROB-287)

`auto_trader`는 결정적 evidence + persistence 레이어, Hermes는 LLM reasoning + composition. 4개 MCP tool (`investment_report_prepare_bundle` / `..._get_hermes_context` / `investment_stage_artifacts_ingest_from_hermes` / `..._create_from_hermes_composition`) 와 동일 surface를 HTTP transport로도 제공.

- **MCP tools**: `app/mcp_server/tooling/investment_hermes_handlers.py`
- **HTTP routes**: `app/routers/investment_hermes_http.py` — prefix `/trading/api/investment-reports/hermes/`
- **AuthMiddleware token branch**: `app/middleware/auth.py` — `HERMES_INGEST_PATH_PREFIX` 라인. 토큰 미설정 → 403, 잘못된 토큰 → 401.
- **서비스**: `app/services/investment_stages/{hermes_context,hermes_ingest}.py`
- **런북**: `docs/runbooks/hermes-report-generation.md`

(ROB-986: `app/flows/hermes_bundle_preparation_flow.py` — 미배포·미호출 확인 후 제거됨. bundle 준비는 MCP/HTTP `prepare-bundle` 호출 시점에 ad hoc로 이루어짐, 별도 스케줄 없음.)

**Env / config 게이트 (모두 default off)**:
- `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED` — MCP tools + HTTP endpoints 공통 게이트
- `HERMES_INGEST_TOKEN` / `HERMES_INGEST_TOKEN_HEADER` — HTTP transport shared secret (header default `X-Hermes-Ingest-Token`). 운영 secret manager에 배치, repo에 commit 금지.

**안전 경계**: 모든 endpoint는 service-layer를 통해서만 쓰기. 어떤 경로도 broker/order/watch/order-intent mutation 도달 안 함. PR #898 static import guard가 `app/services/action_report/snapshot_backed/` + `app/services/investment_stages/` 전체에서 in-process LLM provider 재도입 차단.

**운영 활성화 절차**: `docs/runbooks/hermes-report-generation.md` §3 (non-prod) / §4 (prod cutover). 실 Hermes JSON-over-wire round-trip 검증 후 ROB-287 Done.

### investment_report_create item 계약 (ROB-458)

`investment_report_create`의 `items[]` 각 항목 필수/선택 필드:

- **필수**: `client_item_key`(비어있지 않은 str), `item_kind ∈ {action, watch, risk}`,
  `intent ∈ {buy_review, sell_review, risk_review, trend_recovery_review, rebalance_review}`,
  `rationale`(자유 텍스트 근거).
- **watch 규칙**: `item_kind="watch"`이고 `operation ∈ {None, create, modify}`이면
  `watch_condition` + `valid_until` 필수(`operation="review"`면 면제).
- **선택**: `target_kind ∈ {asset, index, fx}`(기본 `asset`) — **`item_kind`와 별개**이며
  watch 스캐너의 asset/index/fx dispatch용. (자산종류이지 항목 종류가 아님.)
  `decision_bucket ∈ {new_buy_candidate, open_action, completed_or_existing,
  deferred_no_action, risk_watch}`, `side ∈ {buy, sell}`, `symbol`, `confidence`,
  `evidence_snapshot`(비정형 dict) 등.

잘못된 item은 단일 응답으로 모든 위반을 반환한다
(`{success:false, error:"invalid_items", item_errors:[...], required_fields, enums, notes}`).

### get_news 관련성 파이프라인 (ROB-491)

KR `get_news`는 네이버 피드를 `news_articles` + `symbol_news_relevance`에
set-difference upsert하고 DB 상태로 응답한다 (excluded만 제외, pending은 상태
표시). 관련성 판정은 외부 Job이 token-authed ingest로만 write-back —
**auto_trader 코드는 어떤 기사도 자동 제외하지 않는다** (하드코딩 노이즈
블랙리스트 금지). status는 서버 파생: `unrelated` 또는 `low` → `excluded`.

- **모델**: `app/models/symbol_news_relevance.SymbolNewsRelevance`
- **저장 서비스**: `app/services/symbol_news_store.py` — 모든 쓰기는 이 모듈 경유
- **라우터**: `app/routers/news_relevance.py` — GET `pending` / POST `ingest/bulk`
  (`NEWS_RELEVANCE_INGEST_TOKEN`, default-off, GET도 토큰 필요)
- **런북**: `docs/runbooks/news-relevance-judgment.md`
- **스케줄러 연결 없음**: 판정 Job은 레포 밖(Hermes류 세션/operator)
- **주의**: 공유 `KR_INVEST_KEYWORDS`(ROB-169 브리핑 스코어러 공용)에 hint용
  키워드를 추가하지 말 것 — ROB-491 로컬 텀은 `symbol_news_relevance.py`의
  `_KR_EXTRA_INVEST_HINT_TERMS`에만
- **ROB-510**: US/crypto(Finnhub)도 동일 DB 파이프라인 합류 (feed_source
  `finnhub_company_news`/`finnhub_general_news`). Finnhub fetch는
  `FINNHUB_NEWS_TIMEOUT_S`×`FINNHUB_NEWS_MAX_ATTEMPTS` 재시도, 전 실패 시
  degraded + DB stale 폴백.


## 유지 규약

새 기능·안전 경계·계약을 추가하거나 변경할 때에는 관련 분야 계약을 같은 PR에서 갱신하고, 필요하면 두 진입점의 최소 하드룰과 명시적 필독 목록도 함께 갱신한다.
