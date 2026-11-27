# MCP·분석 계약

이 파일은 작업 분야와 무관하게 작업 시작 전에 끝까지 읽어야 하는 필독 계약의 일부다. 찾아보기는 탐색 보조일 뿐 선택 읽기 면제가 아니다.

## 목차

- MCP 표면 계약
- Runtime LLM ownership boundary
- Investment Report Item Contract
- Hermes Report Generation (ROB-287) — retired (hk 6930)
- investment_report_create item 계약 (ROB-458)
- get_news 관련성 파이프라인 (ROB-491)

## 기준 원문 계약

### MCP 표면 계약

레인 allowlist 정본: `config/mcp_lane_allowlists/` (`tool<TAB>basis` 보존).
계약 테스트: `tests/mcp_server/test_lane_allowlist_contract.py` — 레인별 배정 프로필의 실제 등록 합집합 검증.
등록 스냅샷: `tests/mcp_server/test_profile_tool_snapshot.py`; D 제거·C niche 관측: `docs/runbooks/mcp-surface-cleanup-20260905.md`.
live 프롬프트↔서빙 표면 계약(#1003, ROB-1436 이후 DEFAULT): `tests/mcp_server/test_live_prompt_profile_contract.py` + 핀 `tests/fixtures/live_prompt_tool_requirements.yaml` — live 프롬프트가 served/required 로 기록한 도구가 DEFAULT(게이트 on)에 등록돼 있지 않으면 실패한다(strict, xfail 없음). 도구 정리로 프롬프트 필수 도구를 등록 해제하면 이 테스트가 막는다. 절차: 운영자 레포 프롬프트가 바뀌면 핀을 새 operator commit 으로 재고정.

**MCP 유닛 은퇴 (ROB-1436, 2026-11-01, 운영자 결정)**: 고정 유닛 `at-mcp-analysis-readonly`(8768)·
`at-mcp-account-read`(8769)·`at-mcp-tradingcodex-execution`(8770)·`at-mcp-paper-001`(8771)·
`at-mcp-live-kr/-us/-crypto`(8773-8775)를 당시 배포 정의에서
제거했다(근거·소비자 표·정지 순서·롤백은
`docs/runbooks/mcp-unit-retirement-rob1436.md`). 프로필 `analysis_readonly`·`account_read`·`tradingcodex_execution`·
`live-kr`·`live-us`·`live-crypto` 와 그 registrar·`config/mcp_profiles/live.yaml`·fable-workbench 레인 allowlist 는 삭제됐고
이 이름의 `MCP_PROFILE` 은 미지 프로필로 기동 거부된다. 🔴 이제 레포에 **물리적 읽기 전용 MCP 표면이 없다** — 외부 읽기 세션이
필요해지면 새 리뷰로 추가한다(운영자 결정). DEFAULT 는 주문 도구를 등록하므로 읽기 전용 대체물이 아니다.
live 세션은 prefect `KR_LIVE_MCP_MODE=shared`(기본)로 DEFAULT 를 쓴다 — 🔴 `dedicated` 로 바꾸면 사라진 8773-8775 에 붙으려다
실패하므로 prefect 쪽 dedicated 분기 제거는 별도 후속이다. `hermes-paper-kis` 프로필은 유닛만 은퇴하고 코드는 남는다(레인
claude-mock/orch-mock 계약, 운영자 H3-KR 선언, Task 881 도구). 은퇴한 포트는 새 리뷰 없이 재사용 금지.
**MCP 프로필 은퇴 (hk 6456, 2026-11-25, 운영자 결정 `hk:doc decision/2026-11-25/at-ncp-retire-unused`)**: ncp 에서만
돌던 8772(Kiwoom 모의 전체 표면)·8776(H3 crypto paper)·8777(H3 US paper) 고정 유닛의 프로필·H3 registrar·H3 전용
paper-execution route 계약·`krb1-cycle` 레인 allowlist(전용 프로필이 은퇴한 Kiwoom 프로필, KR-B1 은 2026-09-24 정지)를
삭제했다. 그 이름의 `MCP_PROFILE` 은 미지 프로필로 기동 거부된다(#1189). `kiwoom_kr` 은 8772 유닛이 쓰던 프로필이 아니고
`scripts/mock_session_mcp.py`(세션 소유 stdio)가 쓰므로 남는다. DEFAULT 의 `kiwoom_mock_*`·`kiwoom_mock_us_*` 게이트 등록,
Kiwoom 모의 서비스·원장·제안 경로는 그대로다. 같은 task 의 설계 세션 결정으로 그 배포 스크립트와 테스트도
삭제했다(배포는 `deploy/shiftgate/pi/`; `ops/ncp/` 는 hk 6448 에서 삭제됨). 8772·8776·8777 도 새 리뷰 없이 재사용 금지.
🔴 `app/services/brokers/credential_firewall` 검사는 KIS·Toss·Upbit 클라이언트에 남는다(지금 켜는 표면은 없다) —
이 방화벽 검사를 KIS/Toss/Upbit 클라이언트에서 제거하지 마라. 계약 테스트:
`tests/services/brokers/test_credential_firewall.py`.
**저트래픽 프로필 지연 import (ROB-1436)**: `app/mcp_server/lazy_imports.py` — `kiwoom_kr`
프로세스는 `main.py` 가 tooling import 전에 pandas·numpy·yfinance·exchange_calendars·
finnhub 를 placeholder 모듈로 등록해 첫 속성 접근(첫 사용 도구 호출) 때 실제 import 한다.
DEFAULT·그 밖 프로필은 eager 그대로다. 도구 출력 불변. 🔴 모듈 최상위에서 이 패키지들의 속성에 접근하지 마라
(`from pandas import X`, `from __future__ import annotations` 없는 `pd.DataFrame` 어노테이션, `pd.Timestamp(...)` 상수) —
`tests/mcp_server/test_lazy_imports.py` 가 지연 프로필 등록 후 실행된 패키지가 0개인지 검증한다. 측정:
`scripts/mcp_profile_import_rss.py --compare`.
**route_request kr/us 시장 보충 허용 (hk 1040, 운영자 10-16 dr-1040-1 A)**: `route_request_lanes.MARKET_EXTRA_ALLOWED`
= kr·us 에 `toss_get_orderable_cash` 하나. robin-prefect 가 KR/US live rep argv 에만 이 도구를 싣는다(`LIVE_KR_US_ONLY_TOOLS`).
그래서 kr/us route 는 모든 lane 에서 이 도구를 `allowed_tools` 에 두고 `blocked_actions` 에서 뺀다(시퀀스 step 아님, 레지스트리 교집합 유지).
crypto route 는 계속 차단한다. 레지스트리 분할(`STATUS_HELPER_TOOLS`·`MUTATION_TOOLS`)은 그대로이고, 브로커 호출은 Toss `GET /api/v1/buying-power`(+`/accounts`)뿐이다.
`test_route_request_profile_golden.py` 는 이 한 도구 이동을 되돌린 바이트가 main golden 과 같음을 검증한다
(default 의 kr/us 케이스만 이동; tradingcodex_execution·live-kr/live-us 는 ROB-1436 에서 은퇴).
**MCP_PROFILE 필수 (#1189)**: 서버 진입점은 `require_mcp_profile` 로 프로필을 해석하며 비었거나·없거나·공백뿐인
`MCP_PROFILE` 은 DEFAULT 로 폴백하지 않고 기동을 거부한다(DEFAULT 는 `MCP_PROFILE=default` 로 명시). 레포 안 모든
서버 정의의 명시 프로필은 `tests/mcp_server/test_mcp_profile_required.py` 가 목록으로 검증한다. 이 폴백을 되살리지 마라.
**dots 외부 접속 (hk 3592, 운영자 결정 `hk:doc decision/2026-11-08/at-mcp-dots-all-tools`)**:
ChatGPT 세션 dots 가 `https://at-mcp.robinco.dev/mcp` 로 **DEFAULT 표면 전체**(live 계좌 주문 도구 포함)를
쓴다(운영자 답 A). 이 면에 한해 위 ROB-1436 의 "외부 읽기 세션이 필요해지면 새 리뷰로 추가한다" 문장을
대체한다 — 읽기 전용 표면이 생긴 것이 아니다. Cloudflare Access(self-hosted 앱, MCP OAuth, 정책 = 운영자 이메일)가 엣지에서
OAuth 발견 문서와 401 챌린지를 서빙하고 인증된 요청에 `Cf-Access-Jwt-Assertion` 을 붙여 원점으로 보낸다.
원점이 받는 자격증명은 **정확히 둘**이다: (a) 기존 `MCP_AUTH_TOKEN` bearer(동작 불변), (b) 검증된 Access assertion
(`app/mcp_server/cf_access.py`: RS256 서명을 `https://<team>/cdn-cgi/access/certs` 키로, `iss`·`aud`·`exp`·`nbf`(skew 30초),
이메일 정확 일치 allowlist — 키는 10분 캐시, 모르는 `kid` 는 60초에 한 번만 갱신, 갱신 실패는 거부).
결정 함수는 `DualCredentialAuth` 하나이고 streamable-http 와 sse 가 같은 provider 의 미들웨어를 쓴다. assertion 헤더가 있으면
그것이 검증돼야 하며 옆에 유효한 bearer 가 있어도 우회되지 않는다. 🔴 `Cf-Access-Authenticated-User-Email` 등 다른 헤더는
읽지 않는다. 설정은 `MCP_CF_ACCESS_TEAM_DOMAIN`·`MCP_CF_ACCESS_AUD`·`MCP_CF_ACCESS_ALLOWED_EMAILS` 셋 전부 미설정이면 꺼짐(동작은 이전과 같음),
일부만 있거나 형식이 틀리거나 `MCP_AUTH_TOKEN` 이 비었으면 **기동 거부**. DEFAULT 가 아닌 프로필은 이 변수를 읽지 않고 bearer 전용으로
남는다(고정 유닛들이 배포 env 파일을 공유하므로 거부가 아니라 무시).
Access 인증 호출은 `caller_agent_id="dots"`·`caller_source="cf_access"` 로 귀속되고(Sentry consumer·제안의 `creator_agent_id`),
클라이언트가 보낸 `x-paperclip-agent-id` 는 무시된다(loss_cut 허용 목록 사칭 방지). 운영자 이메일은 호출 로그 한 줄
(`consumer=dots operator=operator:<email>`)에만 남고 Sentry·DB 에는 없다. 🔴 Access 신원은 live 주문 도구에 닿을 수 있다 — env 게이트, 호출별
`dry_run`/`confirm`, `approval_hash`, 손실매도 가드, 정확 계좌 검사, kis_mock 귀속, W5 inbox, ROB-1340, NHPLUG, 체결 ingest 토큰은
전부 그대로이며 이 변경은 어떤 게이트도 완화하지 않는다. 배포(터널 경로·Access 앱·운영 env)는 별도 운영자 승인 사항이고
승인 전에는 아무것도 노출되지 않는다. 런북 `docs/runbooks/mcp-dots-access.md`, 테스트 `tests/mcp_server/test_cf_access_auth.py`.
Task 881의 `kis_mock_ledger_expire_day_orders`는 별도 registrar로
hermes-paper-kis에만 등록한다. default/live 및 다른 모든 profile에는 없으며
스냅샷과 과거 감사 예외 테스트가 이 물리적 경계를 검증한다. DB-only 도구여도
KIS_MOCK_ENABLED 및 기존 필수 설정 게이트, dry_run 기본 true와 쓰기 confirm
true를 유지한다.

### MCP tool error reporting (hk 6805)

Caller argument mistakes are not Sentry errors. The closed caller-input set is
`ValueError` (which covers `pydantic.ValidationError`) plus
`fastmcp.exceptions.ValidationError`; `ToolError`/`FastMCPError`/`McpError` are
transparent wrappers. `app/mcp_server/caller_input_errors.py` classifies the
live exception in `McpToolCallSentryMiddleware`: a caller-input failure emits
one `mcp.tool.caller_input_error` warning plus one
`mcp_metric metric=mcp_tool_caller_input_error_total ... value=1` counter sample
and the caller still receives the same `ToolError` response. `app/monitoring/sentry.py`
drops both duplicate Sentry records for such calls — the
`fastmcp.server.server` "Error calling/validating tool" log record (dropped for
every tool failure, since the MCP-integration capture carries the same exception
with `mcp_tool_call` context) and the `ToolError`/`ValidationError` event itself
when its whole non-wrapper cause is caller input. A server-side exception keeps
exactly one Sentry event. Keep the live classifier's type set and the serialized
`_MCP_CALLER_INPUT_ERROR_TYPES`/`_MCP_ERROR_WRAPPER_TYPES` in sync.

### Fundamentals provider availability

`get_financials`, `get_valuation`, and US `get_investment_opinions` treat a Yahoo Finance
quoteSummary response as missing fundamentals only when it is HTTP 404 and explicitly says
`No fundamentals data found`, or when the financial statement result is empty. These cases
return `success=true`, `data_state="missing"`, and `reason="no_fundamentals"` with the
symbol and `source="yfinance"`. Other HTTP statuses, timeouts, malformed responses, and
404s without that provider message remain errors; a failed fetch must not be represented as
missing data. When such an error hits one opinion sub-request while another sub-request
still returns usable data, the response keeps that data and lists the failed sub-request in
`partial_errors` (`field` and `error_class` only, no provider message), so the gap is not
silent. Only when no usable data remains is the error raised. US company profiles continue
to use Finnhub.

### Runtime LLM ownership boundary

auto_trader runtime code must not import or instantiate in-process LLM providers
(Gemini/OpenAI/Grok/etc.). LLM judgment belongs to out-of-process MCP consumers
(claude sessions, etc.). The static guard in
`tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py`
scans `app/**/*.py` for forbidden provider imports and deleted provider files.

### Retrospective P&L coverage (ROB-1488)

`get_trade_retrospectives`의 `pnl_status`와 `get_retrospective_aggregate`의
`pnl_coverage`는 기존 행만 읽는 투영이다. `recorded` class는 `realized_pnl` 값 존재
또는 `recorded_other_pnl`(pnl_pct·total_pnl_krw·fx_pnl_krw·security_pnl_usd/krw 중
값 존재)를 센다. 후자는 절대 realized 금액을 안다는 뜻이 아니며 NULL을 채우지 않는다.
`not_applicable`은 position_intake·매수 leg·no-fill outcome이다. intake는 execution이
아니므로 fill 증거 검사보다 먼저 비해당으로 분류한다. no-fill outcome은 명시적인
unfilled·rejected·cancelled에 한정한다. `unknown`은 execution의 fill 증거 부재·
outcome 부재/미인식(`unknown_outcome`)·side 미상, `missing`은 fill 증거가 있는 매도에 위 P&L 필드가 모두
없는 경우다. missing의 원인은 추정하지 않는다.
체결 증거 부재를 P&L 비해당이나 0으로 바꾸지 않으며, 원장·기존 행에 쓰지 않는다.
aggregate coverage는 기존 group/filter에 포함된 행만 센다.

### Investment Report Item Contract

`investment_report_create` / `investment_report_add_items` reject unknown top-level item keys. Use typed fields for current contracts:

- `trigger_checklist`: `string[]`; copied to watch trigger notifications.
- `max_action`: structured execution-plan JSON for watch items. `account_mode` is required when `max_action` is present; it also requires `side` and exactly one of `quantity` or `notional`; optional keys include `amount_krw`, `limit_price`, `limit_price_hint`, and `ladder_level`.
- Do not send `planned_action` as an item key. Hermes payloads derive `planned_action` from `max_action`.

`investment_watch_create` additionally takes a **top-level** `action_mode`
(`notify_only`/`approval_required` only — `preview_only`/`auto_execute_mock`
are nested-`watch_condition`-only). Top-level and an explicitly-sent
`watch_condition.action_mode` must agree; a mismatch is rejected with
`error="action_mode_conflict"` rather than silently coerced. An effective
`approval_required` also requires non-empty `max_action`
(`error="max_action_required"`, `required_fields=["max_action"]`). Both
rejections are single-response dicts on the MCP create path; existing rows,
the scanner, and reprocessing paths are unaffected.

### Hermes Report Generation (ROB-287) — retired (hk 6930)

외부 report composition(Hermes)은 은퇴했다. 다시 만들거나 대체 경로를 붙이려면 새 결정이 필요하다.

- **제거됨**: HTTP transport(`/trading/api/investment-reports/hermes/*`, 토큰 분기·ingest 토큰 설정 포함, hk 6933). MCP 도구 5개 — `investment_report_prepare_bundle`, `investment_report_get_hermes_context`, `investment_report_create_from_hermes_composition`, `investment_stage_artifacts_ingest_from_hermes`, `investment_report_prepare_intraday_context` — 와 서비스(`investment_hermes_handlers`, `investment_stages/{hermes_context,hermes_ingest,symbol_report_ingest}`, `investment_dimensions/dimension_report_ingest`), 스키마(`schemas/{hermes_composition,investment_symbol_reports,investment_dimension_reports}`), `scripts/shadow_replay_probe.py`·`scripts/shadow_replay.py` 드라이버(hk 6950).
- **이력으로 동결**: ingest 전용 테이블 `investment_dimension_reports`·`investment_symbol_intermediate_reports` 는 읽기 전용 이력이다(writer 를 호출하는 코드 없음). 공유 `investment_stage_runs`/`artifacts`·`investment_report_news_citations`, DB 모델, CHECK 값 `hermes`, 저장된 `HERMES_ADVISOR` 프로필 값도 그대로다. 마이그레이션·drop 없음. dimension report 읽기 라우트는 남는다.
- **유지**: `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED`(`investment_report_generate_from_bundle` 와 investment_reports router 게이트, 기본 off), 라이브 watch notifier `app/services/hermes_client.py`, `hermes-paper-kis` 프로필과 Task 881 도구. 뉴스 citation·composition 형태는 `app/schemas/investment_report_composition.py` 에 있다.
- **안전 경계(불변)**: in-process LLM provider 금지(ROB-501, 정적 import guard).
- **런북(은퇴 기록)**: `docs/runbooks/hermes-report-generation.md`

### Report writer diagnostic (ROB-1488)

`scripts.diagnose_investment_reports`는 READ ONLY·8초 timeout으로 report 시각과
watch의 resolved/null/dangling link를 집계한다. 최근 report-linked event 부재는
모든 의존성 부재 증명이 아니다. H4 운영자 결정은 create/list/get 유지, 비활성
Hermes composition/write 경로의 R4 은퇴이며 writer 활성화·backfill은 없다.
이 진단은 report나 watch를 생성하지 않고 스케줄러에 연결하지 않는다.

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
- **enqueue 없음 (hk 5753)**: `get_news` 는 판정 task 를 TaskIQ 로 enqueue 하지 않는다. 대기 행은 응답의 `pending` 으로 남고, 판정은 외부 루프(GET `pending` → 판정 → ingest)가 한다. task 정의와 `run_news_relevance_judgment` 본문은 운영자 수동 호출용으로 남는다.
- **주의**: 공유 `KR_INVEST_KEYWORDS`(ROB-169 브리핑 스코어러 공용)에 hint용
  키워드를 추가하지 말 것 — ROB-491 로컬 텀은 `symbol_news_relevance.py`의
  `_KR_EXTRA_INVEST_HINT_TERMS`에만
- **ROB-510**: US/crypto(Finnhub)도 동일 DB 파이프라인 합류 (feed_source
  `finnhub_company_news`/`finnhub_general_news`). Finnhub fetch는
  `FINNHUB_NEWS_TIMEOUT_S`×`FINNHUB_NEWS_MAX_ATTEMPTS` 재시도, 전 실패 시
  degraded + DB stale 폴백. `get_news` 최종 에러 응답은 닫힌 사유
  `error_code="news_lookup_failed"`+`error_class`+`suggestion`를 가진다
  (ROB-1384) — '뉴스 없음'과 '조회 실패'를 구분하기 위함.
- **ROB-1388**: Finnhub 뉴스 fetch(company-news·crypto general)도 스레드를
  쓰지 않는다 — attempt마다 `httpx.AsyncClient` GET(`follow_redirects=False`)
  으로 전환해 `wait_for` timeout·호출자 취소 시 요청 코루틴이 취소되고 연결이
  닫히며 기본 executor에 잔여 작업이 남지 않는다(ROB-1384 r5와 같은 패턴,
  공용 `_finnhub_api_key()` 재사용). 엔드포인트는 SDK와 동일
  (`/api/v1/company-news`, `/api/v1/news?category=crypto&minId=0`). API 키는
  `X-Finnhub-Token` 헤더로만 보내고 쿼리에는 넣지 않는다 — strip + 헤더 불가
  문자 검증을 통과하지 못하면 HTTP 요청 전 키 값 없는 ValueError로 실패한다.
  httpx 예외는 정제된 `FinnhubNewsTransportError`/`FinnhubNewsHTTPError`로
  변환하고 raise는 except 블록 밖에서 한다 — `__cause__`·`__context__`가 비어
  원시 예외 체인(키 반영 가능)이 남지 않는다. 추가로 httpcore DEBUG/TRACE
  진단이 원시 프로토콜 오류를 그대로 렌더해 악의 peer가 자격 증명 헤더 값을
  반영한 로그를 만들 수 있으므로, `app.core.logging_config`와 모듈 import
  양쪽에서 `httpcore` 로거를 WARNING으로 제한한다(키를 싣는 요청의 원시
  transport 진단은 어떤 로그 레벨에서도 남지 않는다). 2xx 성공 조건은 닫혀
  있다: 미디어 타입이 정확히 `application/json`이고(부분 문자열 비교 금지 —
  `application/jsonp`·`profile=` 파라미터 우회 불가) 본문이 JSON list이며
  모든 항목이 dict이고 어떤 행도 `error` 키를 갖지 않을 때만 성공 —
  dict(`{"error": …}` 포함)·비JSON·비dict 행·error 행은 재시도
  없는 오류로 fail-closed이고, 행 검증은 limit 슬라이스 전 전체 목록에
  적용된다. 정상 `[]`는 뉴스 0건 성공이다. 재시도 대상은
  timeout·transport·5xx·**429**로 ROB-510 방침을 유지하고(earnings의
  429-무재시도와 다름), 그 밖의 4xx·설정 오류·계약 오류는 즉시 실패한다.
  한 호출의 최악 총시간은 `max_attempts × FINNHUB_NEWS_TIMEOUT_S +
  (max_attempts−1) × 재시도 wait 상한`이며, 요청 timeout·취소 뒤 close가
  멈추면 반환이 예산을 넘을 수 있다 — ROB-1384 R6-F3와 같은 수용 위험이다
  (TLS close는 peer를 기다리지 않고 FD·소켓 누수는 관측되지 않았다). 호출자
  계약(전 실패 시 degraded + DB stale 폴백, 실패를 live로 표시 금지)은
  ROB-510 그대로다. profile/financials 등 여전히 동기 SDK
  (`_get_finnhub_client`)를 쓰는 소비자는 같은 잔여 스레드 위험을 안고
  있으며 범위 밖이다.
- **ROB-1384**: `get_earnings_calendar` US도 동일 신뢰도 계약에 합류한다.
  Finnhub fetch는 `FINNHUB_EARNINGS_TIMEOUT_S`×`FINNHUB_EARNINGS_MAX_ATTEMPTS`
  (per-attempt timeout + bounded retry, 재시도 대상은 timeout/network/5xx만;
  429→`FinnhubQuotaExceededError`·무재시도 유지). 전 실패 시 저장된
  market_events US 적재분을 `degraded=true`+`served_by`/`fallback_source`/
  `data_state`/`stale`/`data_as_of`/`provider_provenance`/`warning`으로
  내려주고, 저장분조차 없으면 `error_code="earnings_lookup_failed"`+
  `error_class`+`data_state="missing"`+`fallback_status`의 닫힌 사유로
  응답한다 — 조회 실패를 '실적 없음(count=0)'으로 오인하지 못하게 하는
  fail-closed 계약이다. `earnings_context` compact ctx는
  `error_code`/`error_class`/`degraded`/`fallback_source`/`fetch_error`를
  전파하고 US 실패/폴백 시 freshness를 `unknown`/`degraded`/`stale`로 표시한다
  (실패를 `live`로 표시 금지). 매수 게이트 판정 로직은 이 계약의 범위 밖이다.
  ROB-1384 r5 범위 결정: 실적 조회는 스레드를 쓰지 않는다. attempt마다
  `httpx.AsyncClient`로 비동기 HTTP 요청을 보내고(`X-Finnhub-Token` 헤더,
  쿼리에 키 없음, `follow_redirects=False`), `wait_for` timeout이나 호출자
  취소 시 요청 코루틴이 취소되어 연결이 닫힌다 — 남는 작업이 없다.
  한 호출의 최악 총시간은 `max_attempts × FINNHUB_EARNINGS_TIMEOUT_S +
  (max_attempts−1) × 재시도 wait 상한`(기본 wait_exponential_jitter max=2s →
  간격당 +2s)이며 정상 응답 뒤의 client close(`async with` exit)도 그 예산
  안에 포함된다. 요청 timeout·취소가 먼저 일어난 뒤 close 단계가 멈추면
  반환이 예산을 넘을 수 있다 — 수용한 위험이다(근거: httpcore의 TLS
  close는 peer를 기다리지 않으며, 고장 주입에서만 재현됐고 FD·소켓 누수는
  없다).
  API 키는 `_finnhub_api_key()`에서 strip + 헤더 불가 문자(제어문자·공백·
  비ASCII) 검증을 거치고, 위반 시 HTTP 요청 없이 키 값 없는 ValueError로
  실패한다 — 변환된 예외는 except 블록 밖에서 던져져 원시 예외 체인에
  원인 예외가 남지 않으므로(`__cause__`·`__context__` 모두 비움) 키가
  남지 않는다. 2xx 응답은 Content-Type이 application/json이고 본문이
  `"error"` 키 없는 dict이며 `earningsCalendar` list를 포함할 때만
  성공이다 — 모든 행은 유효한 `YYYY-MM-DD` date가 필요하고 하나라도
  불량이면 실패다. provider 오류 페이지·오류 객체는 재시도 없는 오류로
  fail-closed. 같은 `to_thread`+slow-drip 위험이 있던 `finnhub_news`
  (ROB-510) 경로는 ROB-1388에서 같은 비동기 HTTP 패턴으로 전환됐다.

### 국내 네이버 뉴스 정기 수집과 발행 시각 (hk 6674)

- 수동 실행: `python -m scripts.collect_kr_news [--dry-run|--commit] [--symbols 005930,000660]`.
  기본은 DB 연결 없는 드라이런이다. 저장 수집은 `symbol_news_store`만 사용한다.
- 매 실행 시장 전체 `https://m.stock.naver.com/api/news/list?category=mainnews&page=1&pageSize=100` 피드
  (`http_naver_mstock_mainnews`)와 작은 국내 종목 집합
  (보유 ∪ 활성 watch ∪ 오늘의 국내 `first_captures`, 기본 상한 30)의
  `https://m.stock.naver.com/api/news/stock/{code}?page=1&pageSize=20` 피드를 수집한다.
  mainnews 기사 ID는 `oid+aid`, canonical URL은
  `https://n.news.naver.com/mnews/article/{oid}/{aid}`이며 Naver의 `dt`는 KST 초 단위다.
  종목별 `datetime`은 KST 분 단위이고 제목 HTML entity를 unescape한다. 기존
  `http_naver_stock_aggregate` 행은 읽기 호환만 유지하며 새 행에는 사용하지 않는다.
  종목 endpoint 행의 `feed_source`는 `naver_item_news`, 보고서의 source label은
  `naver_item_news:{symbol}`이다.
  상한 절단 종목은
  JSON `symbol_cut_list`에 출처와 함께 남긴다. 종목별 Naver 뉴스 조회 실패는 해당 source의 error로
  보고하고 다른 source 처리는 계속한다. 보고서의 source별 `fetched`/`new`/`duplicate`/`errors`는
  정상 빈 응답과 조회 실패를 구분한다.
- `news_articles.published_at_raw`는 제공자 시각 문자열 원문이고 `published_at`은 시간대가 있는 UTC 시각,
  `published_precision`은 `second`/`minute`/`date`다. Naver의 시간대 없는 값은 KST로 해석한다.
  spike attribution은 `http_naver_mstock_mainnews` 중 `published_precision=second`만 KR exact로
  인정하며 minute 정밀도 행은 시각 미확정으로 처리한다. 종목 feed에서 먼저 저장된 같은 기사가
  더 높은 정밀도의 mainnews 시각으로 갱신되면 행의 `feed_source`도 mainnews source로 승격한다.
  기존 aggregate source 행은 과거 clock 계약으로 읽는다.
  해석 실패는 parsed 시각·정밀도 NULL과 `published_at_reason`으로 남긴다. `first_received_at`은 최초
  INSERT 시각으로 URL 재수집에서 바뀌지 않는다. `article_published_at`은 기존 읽기 경로 호환을 위해
  유지한다. 신규 열은 기존 행을 backfill하지 않는다.
- 🔴 **hk 6302 발행 시각 보존**: 기존 `article_published_at`은 시간대 없는 `DateTime`이라 제공자 시간대와
  원문 정밀도가 보존되지 않는다. 정밀한 비교는 `published_at`과 `first_received_at`을 사용하며 기존
  `article_published_at` 소비자와 과거 행은 이 변경으로 재작성하지 않는다.
- 미등록 Dagu 사본: `deploy/dagu/at-kr-news-timing/` — 평일 07:00–20:00 KST, 5분 간격.
  저장소 변경은 DAG를 등록하거나 활성화하지 않는다.

### get_news·get_disclosures 시각, 종목 상태, 사건 키 (hk 6968)

**항목별 시각.** `get_news.news[]`와 `get_disclosures.filings[]`는 항상 `published_at`, `published_offset`,
`published_precision`, `first_received_at`을 싣는다. 모르는 값은 null이며 다른 시각으로 대신하지 않는다.

- `published_at`은 제공자 발행 시각이며 `published_offset` 기준 ISO 8601이다. `published_precision=date`면
  날짜(`YYYY-MM-DD`)만 낸다. 뉴스의 저장 오프셋이 없으면 시장 시간대(KR=KST, 그 밖=UTC)로 표시하고
  `published_offset`은 null이다.
- `first_received_at`은 우리 최초 수신 시각(UTC)이다. 시각 열이 생기기 전 행과 뉴스 저장소가 내려간 응답은 null이다.
- 공시의 발행 시각 근거는 `published_at_source`가 밝힌다. 우선순위: `market_events_publication`(DART 행의
  발행 열) → `dart_receipt_time`(DART 당일 목록의 접수 `HH:MM`, KST, 분 정밀도) → `naver_disclosure`(접수번호가
  연결된 네이버 행) → `dart_list_date`(OpenDART 목록의 날짜). `first_received_at`은 DART 행과 연결된 네이버 행의
  가장 이른 수신 시각이고 `first_received_source`가 출처다. 저장 시각 읽기가 실패해도 공시는 내며
  `receipt_lookup="failed"`다(정상은 `ok`). DART 일별 수집은 새 행에 `first_received_at`을 기록하고 재수집은 바꾸지
  않는다. 기존 행은 소급하지 않는다.

**종목 상태 `symbol_state`** (두 도구, 닫힌 어휘 — `app/mcp_server/tooling/symbol_state.py`). "뉴스 없음"과
"수집 실패"는 다른 상태다.

| 값 | `get_news` | `get_disclosures` |
|---|---|---|
| `ok` | 조회 성공, 응답 항목 중 하나 이상이 3시간 창 안에 수신됨 | 조회 성공, 공시 있음 |
| `no_items` | 조회 성공, 낼 항목 없음(제외 행 수는 `excluded_count`) | OpenDART 상태 `013`으로 증명된 빈 목록 |
| `stale_copy` | 제공자 실패 또는 전 행 판독 불가인데 저장분을 냄, 또는 낸 항목이 모두 3시간보다 오래전에 수신됨 | 내지 않는다(저장분 폴백 없음) |
| `fetch_failed` | 실패이고 낼 것이 없음. 제공자 행을 모두 버린 경우(`parser_skips`) 포함 | 조회·검증 실패 |

- ROB-510의 `error_code="news_lookup_failed"` 오류 응답은 그대로이며 `symbol_state="fetch_failed"`가 붙는다.
- `data_state`/`stale`은 낸 항목 중 **가장 오래된** 수신 시각의 나이이고, `symbol_state`는 이번 조회의 결과다.
  최신 조회가 성공해도 오래된 저장 행이 페이지를 채우면 `data_state=stale`, `symbol_state=ok`일 수 있다.
- OpenDartReader는 DART 오류 상태(한도 초과·키·점검)에도 빈 목록을 돌려주므로, 빈 목록은
  `list.json`의 상태 `013`을 다시 확인해야 `no_items`다. 그 밖의 답은 `success=false`·`fetch_failed`다.
  DART 오류 문자열에서 API 키는 지운다.

**사건 키 `event_key` / `event_key_basis`** (`app/services/news_event_key.py`, 순수 함수, LLM 없음).
점수를 매기는 쪽은 같은 키를 **한 사건으로 한 번만** 센다. 형식은 `ev1:{market}:{symbol}:…`이다.

1. `disclosure_id` — 같은 종목 + 같은 공시 ID(`rcept_no`/`dart_id`, `krx_id`, `kis_id`, `filing_id`, `ir_id`;
   `document_id`류는 제외). 기사가 ID를 정확히 하나 인용할 때만 적용한다(DART 접수번호가 있으면 그것을 쓴다).
   기간 제한은 없다. 공시의 키는 `ev1:kr:{종목}:doc:dart:{접수번호}`라서 그 접수번호를 인용한 기사와 같다
   (종목 코드를 모르면 `-`). 정정 공시는 새 접수번호이므로 다른 키다.
2. `title_day` — 같은 종목 + 정규화 제목 + **시장 현지 같은 날**(KR=Asia/Seoul, US=America/New_York,
   crypto=UTC). 정규화: NFKC, 소문자화, 닫힌 후속 표지 제거(`[속보]`·`(종합)`·`(N보)`·`단독`·`상보`·`수정`·`정정`,
   `Breaking:`·`UPDATE N-` 등), 따옴표·말줄임·구두점 정리. 숫자·부호·`%`·종목 코드는 남긴다. 같은 응답에서
   그 제목·날짜 묶음의 공시 ID가 하나뿐이면 ID 없는 기사도 그 ID 키를 쓴다.
3. `article` — 발행 시각을 모르거나 정규화 제목이 8자 미만이면 기사별 키(URL 해시).

한계: 제목을 새로 쓴 후속 기사와 다음 날 후속 기사는 다른 키다.

**수집 입력 검증(hk 6767 이월).** 뉴스 입력의 `published_offset`은 `±HH:MM`, `published_precision`은
`second`/`minute`/`date`만 받고 그 밖은 배치 저장 전에 422다. 원문에서 유도한 오프셋도 같은 검사를 통과해야
저장한다. 오프셋 우선순위는 명시값 → 제공자 원문 → 파싱 시각이다. `news_article_related_symbols.relation_type`은
`direct`/`sector`/`market` 또는 NULL(미확인)이다. 작성자가 관계와 근거를 함께 주지 않으면 NULL이며
`direct`를 기본값으로 넣지 않는다. 기존 행은 바꾸지 않는다.


### retro/v1 모의 왕복 읽기 `mock_round_trips` (hk 6476)

호환 확장이다 — 새 경로(`/trading/api/retro/v1/mock_round_trips`)와 새 스키마만 더하고 `consumers.lock`은 그대로다(소비자 게이트 통과 확인, 핀 갱신은 호환 확장 → 소비자 커밋 → 핀 갱신 순서). 모의(paper/demo) 전략 journal `review.mock_strategy_orders`의 에피소드 하나가 체인 한 행이다: 진입 leg(`client_order_id = episode_id`)와 같은 episode의 청산 leg. 🔴 읽기 전용 SELECT만 쓰고 `mode="mock"`·`basis="inference"`로 고정한다. 라이브 reader는 건드리지 않았으므로 기존 골든은 바이트 동일하다.

- **상태**: 닫힌 어휘 `closed | open | unresolved | not_filled`. 미확정 leg(`sending`/`uncertain`)가 있으면 `unresolved`+`uncertain_leg`, 청산이 끝났는데 실현 손익이 없으면 `closed`+`pnl_unrecorded`, 진입 leg 없이 청산 leg만 있으면 `unresolved`+`incomplete_evidence`다. 사유 없이 0으로 채우지 않는다.
- **금액**: 진입가·청산가·실현 손익은 청산 뒤에만 정확한 decimal 문자열로 낸다(청산 전 체인의 손익은 null + 사유). 통화는 venue별 고정값이다.
- **브로커 증거**: 주문번호는 노출하지 않고 leg마다 `broker_order_bound` 불리언만 낸다. ref는 내부 PK의 해시다.
- **상한·실패**: 소스 행이 5,000을 넘거나 읽기가 실패하면 `total=null`+`count_unavailable`과 소스 unknown이며 chain 일부를 내지 않는다.
- **범위**: 필터는 `strategy_id`와 네 모의 venue(`alpaca_paper`·`alpaca_paper_crypto`·`binance_spot_demo`·`binance_futures_demo`)다. kiwoom/KIS 모의 원장은 이 연산 범위 밖이며 후속 작업이다. 기존 `mock_experiments`(집계)와 별개다.
- **테스트·예제**: `tests/services/retro_v1/test_mock_round_trips.py`, `contracts/retro/v1/examples/mock_round_trips.yaml`.

## 유지 규약

새 기능·안전 경계·계약을 추가하거나 변경할 때에는 관련 분야 계약을 같은 PR에서 갱신하고, 필요하면 루트 `AGENTS.md`의 최소 하드룰과 명시적 필독 목록도 함께 갱신한다.


### Factortrail retro/v1 공급자 계약 (hk 3815)

`contracts/retro/v1/openapi.yaml`은 Factortrail 소비자를 위한 auto_trader 소유 HTTP 계약의 정본이다. retro HTTP route는 hk 3818에서 제공하며 MCP 도구는 없다. 응답은 닫힌 projection이며 계좌번호, 브로커 주문번호, 원문 payload와 예외를 내보내지 않는다. 연결에는 불투명한 ref만 쓴다. 조회하지 못했거나 실패한 값과 count는 0으로 바꾸지 않고 null과 사유로 표시한다. 생성 시각과 데이터 cutoff를 분리하고 count는 페이지 길이가 아닌 각 작업의 전체 모집단을 뜻한다. 금액·수량·가격은 정확한 decimal 문자열이다. 계좌 수익률의 외부 흐름 완전성은 서버 측 사실이며 호출자가 선언하지 않는다. 공개 예제는 전부 합성이다.

retro/v1에서 `total: null`은 `unknowns`에 `field=total`, `reason=count_unavailable` 항목을 요구한다. 행의 null 값은 같은 행의 `reason`/`missing_reason` 또는 envelope `unknowns`의 해당 필드 경로로 사유를 표시한다. `as_of` 응답은 생성 시각이며 보유 조회 입력은 `cutoff_date`다.

**retro/v1 읽기 모델 (hk 3816)**: `retro_scoreboard`를 뺀 8개 operation의 reader가 `app/services/retro_v1/`에 있다. `read_<operation>(session, **계약 쿼리 이름)`가 계약 응답 본문 dict를 그대로 돌려준다. `retro_scoreboard`는 hk 3817이다. HTTP route는 hk 3818에서 이 reader와 별도 scoreboard reader를 호출한다. MCP 도구와 스케줄러 연결은 없다.

`stage=order`는 세 live 주문 레저를 원천으로 하며 직접 주문도 포함한다. 번호와 멱등키 또는 correlation ID 및 거래일이 일치하는 유일한 rung만 연결하고, fill은 거래일이 일치하는 레저 주문에 연결한다. 상태는 `filled`, `partially_filled`, `working`, `cancelled`, `expired`, `rejected`, `unresolved`로 닫혀 있다. 유효한 권위 fill이 우선하고, 다음으로 브로커 terminal 상태, #1112 rung 추론을 사용한다. 당일 미종결은 `working`, 과거 미종결은 `unresolved`다. 거절 사유는 `rate_limited`, `price_limit`, `insufficient_balance`, `market_closed`, `invalid_order`, `broker_other`다. 만료는 `day_order_expired` 또는 `expired_inference_no_broker_original`, 취소는 `cancelled`, 미해결은 `no_terminal_evidence`다. 원본 브로커 메시지와 코드는 노출하지 않는다. funnel의 order와 fill은 chain과 같은 행을 센다.


- **읽기 전용은 구조다**: reader마다 자기 `REPEATABLE READ READ ONLY` 트랜잭션(statement timeout 8초)을 열어 count와 행이 한 스냅샷을 쓴다. 이미 트랜잭션 중인 세션은 `validation_error`로 거부한다(호출자 트랜잭션을 롤백하는 것도 부작용이다). SELECT 외 문장·브로커 클라이언트·주문·제안·recorder·reconcile·refresh·lazy-fill·MCP·router·스케줄러 import는 `tests/services/retro_v1/test_guards.py`가 직접 import, 정적 transitive closure, 쓰기/commit 호출, 실제 발행 SQL 네 겹으로 막는다. `account_value` 패키지(hk 3817)도 import하지 않는다.
- **unknown은 0이 아니다**: 하위 읽기는 SAVEPOINT 안에서 돌고 실패하면 `unknowns`(`upstream_error`/`unavailable`)와 `total: null` + `count_unavailable`이 된다. 소스당 5,000행(렛저 이력 20,000행) 상한에 닿아도 같다. 행의 null은 행 `reason`/`missing_reason` 또는 `data.<i>.<field>` unknown으로 설명한다(계약 테스트와 같은 규칙).
- **원장 규칙은 재사용한다**: 체결은 SQL의 `execution_ledger_in_effect()`에 파이썬 재검사를 더하고 `supersede_provisional`(ROB-1379)로 websocket 중복을 뺀다. 격리 행과 덮인 중복은 fill·markout·lot·position·funnel 어디에도 없다. `retro_positions`는 브로커 호출 없이 원장에서 `kis_lots.fifo_position`(최신 seed 기준 FIFO)로 보유를 구한다. 그래서 `basis=inference`이며, 미확인 websocket 행·oversold·잘린 읽기가 있으면 `lot_state=unknown`과 null 수량·평단·미실현이다. 브로커 보유 수량과의 대조(`kis_lots` 로더)는 하지 않는다.
- **체인 체결 시각**: 주문을 대표하는 권위 행이 하나이고 같은 order identity의 in-effect websocket 행이 모두 `broker_fill_time`이며 중복 제거 후 수량 합이 권위 행과 일치하면, 체결 행의 `event_at`은 마지막 브로커 체결 시각이다. 그 외에는 권위 행의 시각과 근거를 쓴다. 선택 필드 `fill_time_basis`는 `broker_fill_time`, `broker_order_time`, `observed_at` 중 하나이며 근거가 없으면 생략한다.
- **체인 연결**: `linked_ref`는 항상 상류(직전 단계)를 가리킨다(candidate→proposal→rung→order→fill→markout/lot). 후보→제안 연결은 저장돼 있지 않으므로 같은 종목에서 제안 이전의 가장 늦은 후보로 추론하고 `basis=inference`다. 주문 단계는 rung의 `broker_order_id`가 있을 때만 있고 시각은 `validated_at`이다. 주문↔체결 연결은 `order_identity`(ROB-1379: broker·종목·side·정규화 번호·거래일)와 같은 식별자로 하고, KIS의 거래일은 rung의 `validated_at`(취소·체결 전이가 덮어쓴다)이 아니라 전송 직전 한 번 삽입되는 `review.order_send_intents.created_at`(rung `idempotency_key`, `account_scope=kis_live` 행만 — mock 등 다른 scope의 같은 키는 증거도 모호성도 아니다)에서 얻고, 그 증거가 없거나 모호하거나 같은 식별자가 둘이면 연결은 null(`no_linked_order`)이며 funnel의 fill 건수는 null이다. opening seed(`SEED-*`/`seed_kind=opening_lot`)는 체결·markout·funnel 건수에 없고 lot FIFO 이력에만 쓴다. markout은 체결 시각 이후 DB 일봉의 20번째 종가이며 한 venue의 봉만 센다(KR은 KRX 우선, 그 밖의 다중 venue는 `reason=source_mixed`). 그 봉이 없으면 행을 빼지 않고 `reason=missing_data`로 낸다. lot은 체결이 있는 (broker, 종목)마다 창 끝 FIFO 잔량 한 행이다. 실거래(`account_mode=live`)만 읽는다.
- **ref와 개인정보**: ref는 종류와 내부 PK의 해시(`kind-<sha256 앞 20자>`)이고 브로커 주문번호·계좌번호는 입력이 아니다. 주문번호는 체결↔rung를 잇는 내부 조인 키로만 쓰고 응답에 없다. 금액·수량·가격은 `Decimal`→문자열이며 float는 `TypeError`다.
- **작은 약속**: 응답 `timezone`은 `KST`다(계약 패턴이 IANA 이름의 `/`를 허용하지 않는다). 요청 날짜는 KST 달력일, 시각은 UTC RFC 3339다. 계약 쿼리 이름 `from`은 파이썬 예약어라 `from_=` 또는 `**{"from": ...}`로 받는다. 커서는 쿼리 파라미터에 묶인 불투명 오프셋이다.
- **`get_bars`**: 일봉(kr/us/crypto)과 1분봉(kr/us/crypto) 테이블만 읽는다. 5m·60m은 `unsupported` unknown이다(빈 성공이 아니다). 🔴 kr/us 1분봉은 공개 서빙 표(`public.*_candles_1m`, writer 은퇴)가 아니라 hk 6145 research 표(`research.kr_candles_1m`·`research.us_candles_1m`)만 읽는다(hk 6244). 행은 (time_utc, symbol, venue) 하나이고 `time`은 봉 시작이며 KRX와 NTX 봉은 합치지 않는다. 행의 `source`는 저장된 값(KIS|TOSS|YAHOO|UNKNOWN)이다(crypto 분봉도 저장된 source). 선택 필드(호환 확장, 일봉·crypto 분봉에는 없다): `session_segment`(저장된 세션 라벨), `value`(정확한 decimal 문자열 — 저장값이 NULL이면 키를 내지 않고 `value_reason=unavailable`이며 0으로 채우지 않는다), `first_received_at`(`retrieved_at`, `batch_id`가 `%backfill%`·`promote-%`가 아닌 첫 수신 행만 — 그 밖에는 키 없이 `first_received_reason=unavailable`). 🔴 요청 창이 그 심볼의 research 표 첫 봉(any venue)보다 앞서거나 그 심볼의 행이 표에 없으면 빈 성공이 아니라 `unknowns`에 `data`/`missing_data` 항목을 내고(`total`은 기존 규칙대로 읽은 행 수), 첫 봉 조회가 실패하면 `upstream_error`/`unavailable`이다. research 표는 수집기 시작 이후·제한된 대상 집합만 담으므로 결손은 장 휴장과 구별되지 않는다 — 추측하지 않는다. KR 일봉은 조정 여부를 저장하지 않아 `adjusted=null`+`unavailable`이다. 한 행 안에서 소스를 섞지 않는다. (hk 6264) crypto 일봉·1분봉 창에 행이 하나도 없으면 빈 성공이 아니라 `unknowns`의 `data`/`missing_data`이며 `total`은 0이다(crypto는 coverage 표가 없어 이 규칙이 빈 창 전체에 적용되고, KR/US 1d의 빈 창은 바뀌지 않았다). `market=crypto`에서 `SAHARA`·`sahara` 같은 기본 심볼은 `app/core/symbol.py::to_upbit_symbol`로 `KRW-SAHARA`가 되어 그 키로 조회하고 응답 `symbol`도 같은 값이다. `market`이 없으면 추론은 바뀌지 않는다.
- **KRX 단일가 정정 (hk 6757)**: KR 1분봉에 정정 행이 있으면 그 OHLCV·거래대금·출처(KIS)를 제공하고 `first_received_at`과 `revised_at`을 함께 낸다. 정정된 15:30 KRX 행에만 `krx_official_close`(정확한 원화 decimal 문자열)를 추가한다.
- **`data_gaps`**: 새 probe 없이 기존 점검만 쓴다 — KRX 애프터마켓 적격 목록 신선도(`load_krx_after_market_health`), KIS reconcile 신선도(`compute_freshness` 90분 규칙), 시장별 음성 클래스 기록 건강(`load_negative_class_health`). fresh이면 행이 없고, 점검 자체가 실패하면 `unknowns`와 `total: null`이다.
  ROB-3873 데이터셋: 각 소스는 자기 guarded 하위 읽기로 돈다. 실패는 그 데이터셋의 `unknowns` 항목과 `total: null`이 되고 다른 행은 유지된다. `crypto_insight.fear_greed`·`.stablecoin_supply`·`.defi_tvl`은 레짐 뷰 신선도 규칙(`_crypto_regime.py`에서 복사, 테스트로 고정)을 쓴다. `.breadth`는 최신 스냅샷의 값이 null이면 `unsupported` 행이며, 값이 있으면 같은 레짐 뷰 신선도 규칙을 쓴다. `crypto_insight.aggregate_oi`는 레짐 뷰가 disabled로 보고하므로 항상 `unsupported` 행이다. `candles_1m.kr|us`·`candles_1d.kr|us|crypto`는 최신 봉 날짜를 마지막으로 확정된 세션과 비교한다(crypto는 마지막으로 닫힌 UTC 일봉). 🔴 `candles_1m.kr|us`의 최신 봉은 research 분봉 표(`research.kr_candles_1m`·`research.us_candles_1m`)의 `MAX(time_utc)`다(hk 6244, 공개 `public.*_candles_1m`은 읽지 않는다). `market_events.<source>.<category>.<market>`는 신선도 서비스 행과, 7일 창에 파티션이 없는 기대 소스를 포함한다. `latest_failed_error` 텍스트는 내보내지 않는다. `research_reports`, `valuation_snapshots.kr|us`도 포함한다. 사유는 닫힌 어휘만 쓰며 헬퍼의 오류·예외 문자열은 행에 들어가지 않는다.
- **테스트**: `tests/services/retro_v1/`(가짜 세션 + 계약 응답 스키마 검증). DB 왕복은 CI 몫이며 로컬에서는 돌리지 않았다.

**회고 지표 순수 계산 (L8, hk 4189)**: `app/services/retro_v1/metrics.py`는 판단 rung 입력으로 매수 위험 금액을 rung별 합산하고, 입력이 하나라도 빠지면 부분합 대신 `null`과 닫힌 사유를 반환한다. 음수 손절가는 거부하고 0은 허용한다. 일봉 MFE/MAE는 진입 세션과 청산 세션을 제외한 완전한 세션만 사용하고 0 아래로 내려가지 않는다. 청산 세션이 진입 세션보다 이르면 거부하며 같은 세션 청산은 허용한다. 완전한 세션이 없으면 `no_complete_session`이다. 각 세션에는 최대 한 봉만 허용하며 중복 세션은 거부한다. 세션 달력에 따른 연속성 판정은 caller 책임이다. 기업행위 조정 여부는 `adjusted=null`, 값 basis는 `inference`로 둔다. 기록 비용과 `round_trip_cost_bps` 기반 추정 비용은 별도 결과이며 추정에는 `rate_source`와 정책 버전/hash를 보존한다. `fee_basis=not_provided`이면 fee placeholder를 기록값으로 취급하지 않고 tax 입력은 유지한다. 기록 비용에 통화가 없으면 금액을 보존하고 `missing_currency` 사유를 반환한다. 이 모듈은 전달받은 `Decimal`·날짜·일봉만 계산하며 DB, 파일, 네트워크, 브로커, writer를 import하거나 호출하지 않는다. 동작은 `tests/services/retro_v1/test_metrics.py`에서 고정한다.

### retro/v1 HTTP 라우트 (hk 3818)

`/trading/api/retro/v1`은 기본 비활성(`RETRO_API_ENABLED=false`) 읽기 전용 HTTP 표면이다.
DB 접근 전에 순서대로 비활성 404, `RETRO_API_READ_TOKEN` 미설정 403,
고정 헤더 `X-Retro-Read-Token` 누락·불일치 401, 허용된 9개 GET 외 403을 적용한다.
세션 쿠키나 다른 머신 토큰은 대체하지 못한다. 각 라우트는 대응 reader 하나만 호출하며
스케줄러·MCP 연결은 없다.

### retro/v1 소비자 핀과 검증 게이트 (hk 3819)

`retro/v1` 공급자 변경은 고정된 외부 소비자(`github.com/mgh3326/factortrail` 커밋 `03c3815`)의 실제 Go 클라이언트 검증 게이트를 통과해야 한다.
- **소비자 고정**: `contracts/retro/v1/consumers.lock`이 지원 소비자 repo, 커밋 SHA, Go 버전, 대상 패키지를 정의한다.
- **검증 하네스**: `tools/retro-consumer-check/main.go`가 기존 고정 소비자의 8개 엔드포인트의 envelope 스키마, decimal/int 정밀도, 계좌/주문 식별자 비노출(개인정보 보호), unknown/사유 규약, 인증 경계(401/403)를 실제 HTTP 호출로 검증한다.
- **실행 러너 및 CI 게이트**: `scripts/ci/run_retro_consumer_gate.py`가 루프백 서버를 띄워 하네스를 실행하며, `classify_changes.py`가 retro 경로 변경 시 `retro-consumer-gate` CI 작업을 트리거한다. `aggregate_required.py`는 게이트 누락 시 fail-closed로 거부한다.
- **운영 런북**: 소비자 핀 갱신 절차(호환 확장 → 소비자 커밋 → 핀 갱신 → 구버전 퇴역) 및 비호환 변경(v2 병행 배포 후 이전) 규약은 `docs/runbooks/retro-consumer-pin.md`를 따른다.



### retro/v1 청산 거래와 회고 (hk 3869)

`retro_trades`는 KST 종료일 범위의 `status=closed`, `account_type=live` journal과
`journal_id`로 연결된 `kis_live`, `toss_live`, `upbit_live` 회고만 읽는다.
`entry_at`은 journal 생성 시각이며 `return_rate`는 기록된 `pnl_pct`의 퍼센트다.
지원 시장 외 instrument도 행을 보존하며 시장·통화는 null과 `unsupported`로 표시한다.
금액은 `total_pnl_krw`, `security_pnl_usd`, `security_pnl_krw` 순서로 기록된
값 하나를 선택하고 통화를 함께 제공한다. 수치는 세션 기록이므로 `basis=inference`다.
전체 필터 집계는 페이지와 무관하며 값이 없거나 읽기가 불완전하면 null과 사유를
반환한다. 회고 prose는 민감 패턴을 검사한 뒤 1,000자로 제한하고 차단 시 `redacted`를
기록한다. JSONB와 원본 식별자는 공개하지 않는다. 기존 consumer pin과 8개 호출
하네스는 호환 확장 순서에 따라 유지한다.

### retro/v1 판단 연결 읽기 (L7, hk 4188)

설계 `docs/design/4154-candidate-to-retro-linking.md` §3·§8.1의 reader 규칙이다. 계약에는 선택 필드만 더했다
(기존 필드·열거는 그대로, 값이 없으면 키를 내보내지 않는다). 소비자 pin(`consumers.lock`)은 L10이 갱신한다.

- **ChainRow**: `link_basis`(`recorded`\|`inference`, `linked_ref`를 만든 방식 — 기존 `basis`는 값 basis이고 그대로),
  `decision_ref`(proposal·rung·order 행이 내려오는 판단의 불투명 ref), `decision_link_basis`, `link_reason`(닫힌 코드:
  `ambiguous_attempts`·`attempt_unconfirmed`·`no_candidate_ref`·`candidate_outside_trading_day`·`candidate_ref_unresolved`).
  `decision_link_basis`·`link_reason`은 설계 §8.1의 두 필드에 더한 보조 필드다(한 행에서 후보 링크는 추론이고 판단 링크는
  기록일 수 있고, 행의 `reason`은 주문 상태 사유가 쓴다). rung→proposal·fill→order·markout→fill은 `recorded`, lot은 FIFO라 `inference`다.
- **주문→판단 (§3 4행)**: `links.resolve_attempts`. 한 `send_key`의 시도 중 `proven_not_sent`가 없는 것의 판단 집합이
  둘 이상이면 링크 없음(`ambiguous_attempts`, 레거시 rung 매칭도 쓰지 않는다), 하나이고 그 시도 중 `sent` 결과가 있으면
  `recorded`, 없으면 `inference`(`attempt_unconfirmed`)다. 살아 있는 시도가 없으면 레거시 규칙: rung `idempotency_key`가
  원장 키와 같으면 `recorded`, correlation만 같으면 `inference`이고 판단은 그 rung 제안에 저장된 `decision_id`다.
  결과 행 유실은 `recorded`를 막을 뿐 만들지 않는다. 🔴 시도·결과 읽기가 실패하거나 상한에 닿으면 증거가 불완전하므로 주문은 어떤
  링크도 얻지 못한다(`link_reason=attempts_unavailable`, 레거시 rung 매칭 포함 — 빠진 시도가 다른 판단일 수 있다). 이 읽기 실패는
  `unknowns`(`source.decision_links`, `source.decision_link_outcomes`)로도 남고 모집단·`total`에는 영향이 없다.
- **후보→제안 (§3 1행, Q20)**: 제안에 저장된 판단의 후보 ref(`candidate_call_id`+심볼)가 있으면 그 writer 라벨(`recorded`/서버 보강
  `inference`)을 그대로 쓰고 reader가 다시 추론하지 않는다. 판단이 없는 제안의 기존 추론(같은 심볼의 가장 늦은 이전 후보)은
  제안과 **같은 KST 날짜**의 후보로 제한되며 넘으면 링크 없음(`candidate_outside_trading_day`, 이전 후보 자체가 없으면
  `no_candidate_ref`)이다. 판단 행이나 후보 행을 찾지 못하면 `candidate_ref_unresolved`이고 추측하지 않는다.
- **TradeRow**: `decision_ref`·`decision_link_basis`·`link_reason`·`fill_time_basis`·`costs`. 회고에 저장된 `decision_id`는
  `recorded`(L6 writer가 판단 행과 일치를 검증), 서로 다른 둘이면 `ambiguous_decision`. 저장된 것이 없으면 회고 `correlation_id`
  → 그 원장 주문 하나 → 시도 규칙으로 유도하고 항상 `inference`이며 저장하지 않는다(주문이 여럿이면 `ambiguous_decision`, 시도가
  갈리면 `ambiguous_attempts`, 시도 읽기가 불완전하면 `attempts_unavailable`, 못 찾으면 `no_decision_link`). 주문 읽기가 실패하면
  링크 부재를 주장하지 않는다. 🔴 회고 읽기가 실패하거나 상한에 닿으면 새 필드 전부를 내지 않는다(일부 회고만으로 말하지 않는다).
- **체결 시각·비용 (L5 열, §5.3·§6.2)**: 거래의 주문(저널·회고 correlation의 라이브 원장 주문)에 정확한 order identity(KIS만 거래일을
  포함 — 자정을 넘긴 Upbit 체결도 같은 주문이다)로 붙은 체결(`execution_ledger_in_effect()`, ROB-1379 supersede, opening seed 제외)만
  쓴다. 주문 원장이나 체결 읽기가 불완전하면 `fill_time_basis`·`costs`를 내지 않는다. `fill_time_basis`는 주문별 basis 중 가장 약한 것이다:
  주문의 in-effect websocket 행이 모두 `broker_fill_time`이고 수량 합이 권위 행 수량과 같을 때만 그 체결 시각을 쓰고, 아니면 권위 행의
  basis(`filled_at_basis` NULL인 과거 행은 KIS reconciler만 `broker_order_time`, 그 밖은 unknown이라 값을 내지 않는다)를 쓴다.
  `costs.recorded`는 브로커가 준 수수료·세금뿐이고 `fee_basis=not_provided`와 과거 KIS reconciler의 `fee=0`은 기록된 0이 아니다.
  한 체결이라도 수수료를 주지 않았으면 세금이 있어도 부분합을 내지 않고 `recorded=null`(`recorded_reason`)이다. 과거 Toss 수수료는
  commission+tax 합이라 `fee_includes_tax=true`다. `costs.estimated`는 `metrics.calculate_costs`가 정책
  `round_trip_cost_bps`를 진입(매수) 체결 notional에 곱한 값이며 매수 체결이 없으면 `missing_notional`이다(매도 대금으로 대체하지 않는다).
  `rate_source`·정책 버전·hash를 싣고 `recorded`와 섞지 않는다.
- 🔴 이 reader는 쓰지 않는다: 새 읽기는 SELECT뿐이고 체결은 `chain.fills_stmt`를 재사용한다. 마이그레이션·writer 변경 없음. `retro_funnel`은
  링크 테이블을 읽지 않는다. 동작은 `tests/services/retro_v1/test_decision_links.py`·`test_trade_evidence.py`가 고정하고,
  링크 필드가 없는 행은 main 골든(`tests/services/retro_v1/golden/`)과 선택 필드만 다르다.

### retro/v1 판단 회고 읽기 (L9, hk 4190)

`retro_decisions`는 KST `decided_at` 창의 `review.decision_evidence` 행마다 한 행을 내는 설계 §8.2의 호환 확장 operation이다(새 경로·새 행 스키마만 더하고 기존 필드·열거는 그대로 — 소비자 pin은 L10이 갱신한다). 필수 쿼리는 `from`·`to`(최대 366일), 선택 필터는 `market`·`symbol`이고 `limit`·`cursor`는 공통 규칙이다. MCP 도구·스케줄러 연결은 없고 다른 reader와 같은 `REPEATABLE READ READ ONLY` 스냅샷 안에서만 읽는다.

- **판단 행**: `decision_ref`(해시 ref, 내부 id가 아닌 `decision_id` 기준), `decision_kind`(`proposal`/`direct_order`/`declined`), `decided_at`/`decided_by`, 시장·심볼·side·strategy는 저장값 그대로이며 사유 없는 빈 값은 `not_applicable` unknown이다. `candidate_ref`는 판단에 저장된 `candidate_call_id`가 가리키는 같은 심볼의 가장 늦은 후보 pick이며 writer가 기록한 `candidate_link_basis`를 그대로 내고, ref나 후보를 해석할 수 없으면 null과 `candidate_ref_unresolved`/`no_candidate_ref`다.
- **계획 위험(§5.1)**: rung 참조가는 `limit`→`approval_price`→`quote_at_decision`→`candidate_price` 우선순위로 고르고 마지막만 `inference`다. 손절은 판단에 저장된 값(`recorded`)이 우선이고 없으면 제안의 backfilled `lot_exit_plans` 정책 손절을 `inference`로 쓴다(여럿이면 가장 보수적인 최솟값). `planned_risk_amount`·`risk_pct_of_notional`·`risk_pct_of_budget`은 `metrics.calculate_risk_amount` 그대로이고 입력이 빠지면 부분합 없이 null과 사유다. sleeve `budget_cap`은 `strategy_sleeve_versions`의 최신 버전이며 없으면 `no_budget_cap`이다.
- **주문·체결(§3 4행)**: `order_count`·`order_states`는 이 판단으로 해석되는 시도(`recorded`/`unconfirmed`)가 있는 주문 원장 행만 센다 — 같은 `send_key`의 시도가 두 판단으로 갈리면 양쪽 다 연결하지 않는다(애매함은 부정이 아니라 무표시다). 링크 읽기가 실패하거나 상한에 닿으면 `attempts_unavailable`과 `order_count=null`(`count_unavailable`)이다. `entry_avg_price`·`filled_quantity`는 매수 체결에서만 나오고(`_order_entry_notional`과 같은 규칙) `fill_time_basis`·`costs`는 `trade_evidence`의 체결 시각·비용 규칙을 그대로 쓴다.
- **MFE/MAE(§5.2)**: `mfe`·`mae`는 `1`·`5`·`20`·`to_exit` horizon별 `metrics.calculate_mfe_mae`이며 진입 세션(마지막 매수 체결의 KST 일)과 청산 세션(첫 매도 체결)을 제외한 완전한 일봉 세션만 쓴다. horizon `N`은 진입 세션 다음부터의 **거래소 달력 세션** `N`개다(KR=XKRX, US=XNYS, crypto=연속 UTC 일; `app/services/retro_v1/horizon_sessions.py`가 `app/services/market_events/session_calendar`로 센다). 그 세션 각각이 닫혔고 봉이 정확히 하나 있어야 값을 낸다. 결손 세션이면 `unknown`·`missing_data`, 아직 닫히지 않은 세션이 남았으면 `pending`·`window_open`이다. 저장된 봉 개수로 세지 않아 결손이 다음 세션으로 밀리지 않으며, 달력이 분류하지 못한 날은 추측하지 않고 `unknown`이다. 주말·휴장일은 결손이 아니다. `to_exit`은 진입 다음 세션부터 청산 세션까지 같은 규칙이고, 청산 세션이 horizon 안에 오면 그 세션에서 창을 자른다(청산 세션 봉은 값에 쓰지 않는다). 청산 세션이 아직 닫히지 않았거나 봉이 없으면 `to_exit`은 `pending`·`unknown`으로 남는다. `r`은 계획 위험 단위(`planned_risk_amount/planned_quantity`)로 나누고 못 나누면 사유를 낸다. `excursion_basis`는 `inference`, `adjusted`는 `null`이다. `r_multiple`은 (청산 평단−진입 평단)/위험 단위이고 어느 입력이든 없으면 null과 사유다.
- **회고 링크(§3 7행)**: 저장된 `decision_id`가 하나면 `recorded`, 없으면 주문 correlation으로 유도한 유일 회고를 `inference`로 낸다. 둘 이상이거나 갈리면 `ambiguous_decision`, 못 찾으면 `no_decision_link`이고 회고 읽기가 불완전하면 `attempts_unavailable`로 부재를 주장하지 않는다.
- **매도 판단과 side 미기록 (hk 6040, 호환 확장)**: `entry_avg_price`·`filled_quantity`는 매수 체결 전용이다. `side=sell` 판단은 이 둘을 null + `fill_reason=sell_decision`으로 내고(0이 아니다), 이 판단에 연결된 주문의 자기 매도 체결(같은 order identity·in-effect·supersede 규칙)은 선택 필드 `sell_filled_quantity`·`sell_avg_price`·`sell_fill_reason`에 낸다(`no_fills`/`no_linked_order`/`missing_notional`/`attempts_unavailable`). 이 선택 키들은 매도 판단 행에만 있고 매수·기타 행의 출력은 바이트 동일하다. `declined` 판단의 side가 NULL이거나(새 writer), 과거 placeholder(side=buy + 수량·금액·지정가가 모두 없는 rung 0)이면 `side=null` + 선택 필드 `side_reason=side_not_recorded`다(hk 5719 `declined_*` 입력이 있는 행은 매수 그대로). rung 읽기가 불완전하면 placeholder로 단정하지 않는다. writer: `forecast_save(decision_side=buy|sell)`은 선택이며 생략하면 declined 판단은 side NULL·rung 없이 기록된다(전에는 항상 buy + 빈 rung 0). `declined_*` 입력과 `decision_side=sell`을 함께 주면 `decision_side_conflict`, 그 밖의 값은 `decision_side_invalid`, `deferred_no_action`이 아니거나 미지원 시장이면 `declined_bucket_required`/`declined_market_unsupported`로 쓰기 전에 거부된다. `decision_evidence`는 append-only라 과거 행을 고치거나 백필하지 않는다.
- **닫힌 projection**: 행의 모든 `*_reason`은 닫힌 `DecisionReason` 어휘이고 null 값은 행 사유 또는 `data.<i>.<field>` unknown으로 설명한다(계약 테스트 규칙). 응답에는 계좌번호·브로커 주문번호·send key·원문 payload가 없다. 동작은 `tests/services/retro_v1/test_retro_decisions.py`가 계약 스키마로 고정한다.

### retro/v1 funnel 집계 범위 (hk 5496)

`retro_funnel`의 각 `FunnelRow`는 선택 필드 `date_basis`(닫힌 어휘 `candidate_recorded_at_kst`·`forecast_created_kst`·`proposal_created_kst`·`order_trade_date_kst`·`fill_filled_at_kst`)를 항상 싣는다. 실계좌 단계(`proposal`·`rung`·`order`·`fill`)는 추가로 `account_mode`(항상 `live`)와 `brokers`(닫힌 `kis`·`toss`·`upbit`의 고유 목록)를 싣는다. 후보와 기각 forecast는 계좌 모집단이 아니므로 이 두 키를 내지 않는다. `brokers`는 그 단계 쿼리가 계좌 모드·레저 scope 조건으로 고른 브로커를 시장별로 좁힌 값이다(KIS·Toss는 KR/US, Upbit은 crypto). 값은 `chain.py`가 쿼리에 바인딩하는 같은 상수(`LIVE_PROPOSAL_ACCOUNT_MODES`·`KIS_ORDER_ACCOUNT_MODE`·`TOSS_ORDER_ACCOUNT_MODE`·`LIVE_ORDER_SCOPES`)에서 나오며 `app/services/retro_v1/funnel_scope.py`가 조립한다. `order` 단계의 날짜 기준은 주문 레저의 `trade_date`이고, KIS 주문의 전송 시각(`order_send_intents.created_at`)은 판단과의 연결에만 쓰이며 집계 창에는 쓰이지 않는다. 체결은 `execution_ledger.filled_at`이다. 계약 필수 목록과 기존 필드·열거는 바뀌지 않았고 `consumers.lock`도 그대로다(호환 확장 → 소비자 커밋 → 핀 갱신 순서, `docs/runbooks/retro-consumer-pin.md`). 테스트 `tests/services/retro_v1/test_funnel_scope.py`는 kr·us·crypto 픽스처에서 보고된 브로커·날짜 기준이 실제로 실행된 SQL의 바인드 값과 WHERE 열과 일치함을 확인하고, 새 키를 지우면 기존 골든 바이트와 같음을 확인한다.

### retro/v1 제출 차단·대기 사유 `block_reason` (hk 5610)

`proposal`·`rung` 단계 chain 행(`retro_day`·`retro_symbol`)에 선택 필드 `block_reason`(닫힌 어휘 `BlockReason`)과 `block_reason_basis`(`recorded`/`inference`)를 더한다. 호환 확장이며 `consumers.lock`은 그대로다(소비자 게이트 통과 확인, 핀 갱신은 호환 확장 → 소비자 커밋 → 핀 갱신 순서). 🔴 원천은 이미 저장된 열뿐이다 — 새 기록·writer·dispatch·승인 경로 변경이 없고 `app.services.order_proposals`는 import하지 않는다(문자열은 `block_reason.py`에서 리터럴로 비교). 증거가 값을 결정하지 못하면 키를 내보내지 않는다(빈 문자열·0·추측 금지). 저장된 `void_reason` 원문, 승인창 `failure_code`의 detail, 브로커 메시지는 응답에 나가지 않는다(매칭에만 쓰고 닫힌 코드만 낸다). `basis=recorded`는 writer가 그 상태·코드를 직접 저장한 경우, `inference`는 "전송된 적 없음" 같은 부재 증거에서 유도한 경우다. 이 필드는 `status`·`reason`을 대체하지 않으며 주문·체결·후속 단계에는 붙지 않는다.

| 값 | 대상 | 유도 규칙(저장 열) | basis |
|---|---|---|---|
| `deferred_session_closed` | proposal·rung | 제안 `lifecycle_state='proposed'`, `approval_dispatch_state ∈ {failed, partial_failed, failed_superseded}`, `approval_dispatch_failure_code`가 `DEFER_SESSION_CLOSED/…`. rung은 `pending_approval`/`needs_reconfirm`일 때 제안의 값을 따른다 | recorded |
| `no_executable_window` | 동일 | 위와 같고 코드 `NO_EXECUTABLE_WINDOW/…` | recorded |
| `calendar_unknown` | 동일 | 코드 `CALENDAR_UNKNOWN/…` | recorded |
| `invalid_valid_until` | 동일 | 코드 `INVALID_VALID_UNTIL/…` | recorded |
| `expired_valid_until` | proposal·rung | (a) 승인창 코드 `EXPIRED/…`(제안 `proposed`, 위 dispatch 조건), (b) 제안/rung `void_reason`이 `expired_valid_until`로 시작(야간 스윕 등), (c) 제안 `lifecycle_state='expired'`이고 모든 rung(또는 해당 rung)의 `broker_order_id`·`idempotency_key`가 NULL — 브로커에 닿지 않은 만료 | (a)(b) recorded, (c) inference |
| `auto_approve_demoted` | proposal·rung | 제안 `proposed`, `source_asof.auto_approve_rejections`에 rung 목록이 있는 시도가 있고 `source_asof.auto_approved` 키가 없음(자동 전송 안 됨). 위 승인창 코드가 있으면 그것이 우선 | recorded |
| `awaiting_human_approval` | proposal·rung | 제안 `proposed`, `approval_dispatch_state='sent_current'`, `approval_dispatch_card_kind ∈ {manual, reconfirm, loss_cut_confirmation}`, `approval_nonce_used_at IS NULL`. `auto_veto` 카드는 대기가 아니다. 앞의 값들이 우선 | recorded |
| `voided` | proposal·rung | 제안 `lifecycle_state='voided'`, rung `state ∈ {voided, voided_local_stale}` (거부 사유 문자열이 아닐 때) | recorded |
| `superseded` | proposal·rung | `lifecycle_state='superseded'` / rung `state='superseded'` | recorded |
| `denied_by_operator` | proposal·rung | 제안 `lifecycle_state ∈ {voided, rejected}` 또는 rung `state ∈ {voided, voided_local_stale, rejected}`이고 `void_reason`이 정확히 `operator_denied`/`telegram_deny` | recorded |
| `rejected_policy_guard` | rung | rung `state='rejected'`, `void_reason_group='policy_guard'`(거부 사유 문자열 제외) | recorded |
| `rejected_broker` | rung | rung `state='rejected'`, `void_reason_group ∈ {broker_rejection, provider_throttle}` | recorded |
| `duplicate_intent` | rung | rung `state='rejected'`, `void_reason_group='duplicate_pending_intent'` | recorded |

키가 없는 경우: 이미 제출·체결 단계로 간 행(`approved`·`submitted`·`resting`·`filled` 등), 승인창 코드가 아닌 dispatch 실패, 모르는 코드, 이후 카드가 성공 발송된 행, 이미 응답(nonce 소비)한 카드, 브로커에 닿은 주문의 만료, `void_reason_group`이 없는 거부, 제안 단계의 단순 `rejected`. 제안 `rejected`는 rung의 사유가 갈릴 수 있어 거부 문자열(`operator_denied`/`telegram_deny`)일 때만 값을 낸다. 동작은 `tests/services/retro_v1/test_block_reason.py`가 값마다 고정하고, 어휘·계약 enum 일치와 원문 비노출을 함께 검증한다. 소비자 게이트(`scripts/ci/run_retro_consumer_gate.py`)는 새 선택 필드를 포함한 계약으로 통과한다.

### retro/v1 같은 종목·거래일 타임라인 `retro_timeline` (hk 6171)

호환 확장이다 — 새 경로와 새 스키마만 더하고 기존 경로·필드·열거는 바이트 동일하며 `consumers.lock`은 그대로다(소비자 게이트 통과 확인, 핀 갱신은 호환 확장 → 소비자 커밋 → 핀 갱신 순서). 한 날짜·한 시장(선택적으로 한 종목)에서 별도 표에 흩어진 기록을 종목마다 한 행으로 묶어 "왜 매수가 없었나"를 증거로 답한다. 🔴 읽기 전용이다: `app/services/retro_v1/retro_timeline.py`가 다른 reader와 같은 `REPEATABLE READ READ ONLY` 스냅샷(8초 timeout) 안에서 SELECT만 하고, MCP 도구·스케줄러·writer·backfill은 없다. 관측 로그 writer(`invest_momentum_events.intraday`)는 브로커 패키지를 import하므로 retro/v1 import 가드가 막는다 — 두 표(`invest_momentum_event_snapshots`·`invest_momentum_event_runs`)는 이 reader가 직접 SELECT한다. 쿼리는 `market`(kr|us|crypto)·`date`가 필수이고 `symbol`·`limit`·`cursor`는 선택이다.

- **날짜 기준**: 요청 날짜는 모든 소스에서 KST 달력일이다(시각은 UTC RFC 3339). 소스마다 다음 열을 쓴다 — 관측 로그 `trading_date`(KST 날짜, KR만), 픽 로그 `recorded_at`, 판단 `decision_evidence.decided_at`과 `deferred_no_action` forecast `created_at`, 제안·주문·체결은 기존 chain reader(`load_chain` + proposal/order/fill 행 빌더)와 같은 KST 창이다. US 소스 중 ET 거래일을 저장하는 것은 분봉 표(`research.us_candles_1m.session_date`)뿐이라 US 분봉만 그 열이 요청 날짜와 같은 행을 센다. KR 분봉은 `research.kr_candles_1m.time_utc`가 KST 창 안인 행이다.
- **행**: 종목 하나당 한 행 — 어느 소스에든 나타난 종목의 합집합, 요청한 종목은 기록이 없어도 `not_observed` 행이다. 심볼 키는 `app/core/symbol.py` 규칙(`normalize_symbol`)이고 정렬은 심볼 오름차순이다. 필드: `symbol`·`market`·`state`·`reason`·`events`·`minute_bars`(+ 읽지 못했을 때만 `minute_bars_reason`). `total`은 모집단 크기이고 어느 소스 그룹이 실패하거나 상한(5,000행)에 닿으면 null + `count_unavailable`이다.
- **이벤트**: `at`(UTC)·`source`(닫힌 목록 `observation_log`·`pick_log`·`decision`·`proposal`·`order`·`fill`)·`kind`·`reason`·`ref`. `kind`/`reason`은 소스 어휘의 닫힌 코드(`safe_code`)이고 원문 텍스트는 나가지 않는다. 순서는 시각 → source 순서 → ref다. 관측 로그는 종목의 그날 첫 포착 한 건(kind = 순위 면 `up`·`down`·`priceTop`)이고 `ref`가 없다. 픽 로그는 `candidate-…`(kind = 저장된 `admission`, 값이 없는 과거 행은 `unrecorded`), 판단은 `decision_ref`·forecast는 `forecast-…`, 제안·주문·체결은 `retro_day`가 내는 ref와 같은 값이다(`block_reason`이 있으면 제안 `reason`이 된다).
- **상태(닫힌 7개, 우선순위 순)**: `proposed_filled`/`proposed_unfilled` — chain의 제안·주문·체결 행이나 `proposal`/`direct_order` 판단이 있다. 체결 쪽은 chain의 주문 상태(`filled`·`partially_filled`)나 체결 행이 있을 때이고 사유는 `filled`·`partially_filled`·`fill_event_only`, 미체결 사유는 가장 나중 단계의 닫힌 코드(`order_<status>`·제안 `block_reason`/`proposal_<lifecycle>`·`decision_without_order`)다. `admitted_held` — admitted 픽 + `declined` 판단 또는 `deferred_no_action` forecast. `admitted_no_record` — admitted 픽이 있고 어떤 판단 기록도 없다(보류로 추론하지 않는다). `candidate_not_admitted` — 픽이 `not_admitted`/`dropped_preselection`뿐이며 사유는 가장 나중 행의 기록된 `admission_reason`(없으면 그 kind). `observed_only` — 관측 로그 첫 포착만 있고 다른 곳에는 없다. `not_observed` — 요청한 종목이 어느 소스에도 없다.
- **결정한 경계**: ① 픽 행 없이 `declined` 기록만 있으면 `admitted_held` + `declined_without_pick_record`다(기록이 있는 쪽이 `not_observed`보다 정확하다; `not_admitted` 픽이 함께 있으면 `candidate_not_admitted`가 우선). ② 과거 행처럼 admission이 비어 있는 픽은 admitted도 not_admitted도 아니다 — 다른 증거가 없으면 `observed_only` + `pick_admission_unrecorded`. ③ 매도 체결도 `proposed_*`로 센다(이벤트 kind가 `sell_filled`로 구분한다).
- 🔴 **`source_unavailable`**: 소스 그룹(`observation_log`·`pick_log`·`decisions`·`orders`)의 읽기가 실패하거나 상한에 닿으면, 그 그룹이 지금 보이는 상태보다 높은 상태를 만들 수 있었던 행은 `state=null`·`reason=source_unavailable`이고 `unknowns`(`source.<이름>`, `upstream_error`/`unavailable`/`incomplete_evidence`)가 남는다 — 더 약한 상태로 내려가지 않는다. 읽은 이벤트는 그대로 나간다. 보이는 양성 증거(체결 행, admitted 픽 + 판단 등)는 무관한 그룹의 실패에 영향받지 않는다. 판정은 순수 함수 `derive_state`/`assemble`이다.
- **`coverage`(응답 최상위, 선택 아님)**: `observation_log.status` — `runs_recorded`/`observation_log_not_running`(그 KST 날짜에 `invest_momentum_event_runs` 행이 0개; 스냅샷이 없다는 증명은 아니다)/`unsupported_market`(KR 외)/`source_unavailable`, `runs`는 상태별 행 수(`ok`·`empty`·`error`). `pick_log` — `calls_recorded`/`no_calls`/`source_unavailable`과 그날 시장의 서로 다른 `call_id` 수. `minute_bars` — `available`/`unsupported_market`(crypto)/`source_unavailable`. 그래서 "기록이 없었다"와 "기록 장치가 돌지 않았다"가 구분된다.
- **분봉**: 행마다 `minute_bars`는 venue별 `{venue, rows, first_bar_at, last_bar_at}`(봉 시작 시각)이고 가격은 없다. 저장된 봉이 없으면 빈 배열이고 읽지 못했거나 지원하지 않으면 null + `minute_bars_reason`이다.
- **테스트**: `tests/services/retro_v1/test_retro_timeline.py`(상태 7개와 `source_unavailable` 순수 테스트, 가짜 세션 reader·coverage·계약 스키마 검증), `test_guards.py`(import closure·SELECT 전용·쓰기/commit 금지), `tests/contracts/test_retro_v1_contract.py`·`tests/routers/test_retro_v1_http.py`, 예제 `contracts/retro/v1/examples/retro_timeline.yaml`. 소비자 게이트(`scripts/ci/run_retro_consumer_gate.py`)는 새 경로와 무관하게 통과한다.

### retro/v1 계획 소스 `plan`과 매수 무산 사유 (hk 7005)

`retro_timeline`의 호환 확장이다 — `source` 닫힌 목록에 `plan`을 더하고 행에 선택 필드 `plan_reason`을 더하며, 기존 필드·열거·상태 우선순위는 바이트 동일하다. `consumers.lock`은 그대로다(호환 확장 → 소비자 커밋 → 핀 갱신 순서). 🔴 읽기 전용이다: `app/services/retro_v1/plan_source.py`가 같은 `REPEATABLE READ READ ONLY` 스냅샷(8초 timeout) 안에서 `review.daily_plan_*`·`review.execution_intents`·`review.execution_child_requests`·`review.execution_ledger`를 SELECT만 하고, writer·backfill·스케줄러·MCP 도구는 없다. 6790 배분 비교(`versions.metadata.allocation_comparison`)는 운영자 전용(설계 §4.2)이라 **읽기만** 하고 응답에 옮기지 않는다 — 순위 A/C·W/N 배분은 `plan_reason`의 두 불리언으로만 접힌다.

- **이벤트**: 그날 (market, session_date) head의 최신 `PUBLISHED` 버전에서 유효한 행 **row key마다** `published_executable`(execution_state=executable) 또는 `published_watch`, 그리고 `valid_until`이 지났으면 `expired`. 한 종목에 candidate와 holding 행이 함께 있으면 각 행의 이벤트가 모두 그 종목 행 아래에 실린다. `at`은 게시 버전 `created_at`과 `valid_until`이고, `reason`은 행 `action`(`new`/`add`/`watch`/`hold`/`trim`/`risk_sell`), `ref`는 `(plan_head_id, seq, row_key)`에서 만든 불투명 `plan-…`이다. 이벤트는 기존 `state`/`reason` 판정에 참여하지 않는다(계획 축은 별도다). 요청 `symbol`이 있으면 plan 행 읽기도 그 정규화 symbol로 좁힌다.
- **`plan_reason`(닫힌 6개)**: 설계 §6의 증거 열만 쓴다. `candidate_not_captured` — 그 버전의 입력 번들 후보 집합에 종목이 없음(`input_bundles.body.rows`). `condition_not_reached` — `executable` 행이 만료됐고 E1 child가 없음. `data_delayed_or_missing` — 6790 점수 스냅샷의 입력이 `unavailable`이거나 행의 저장된 `not_executable_reason`이 입력 사유. `budget_or_guard_block` — E1 `refused`/`not_sent`의 `refusal_reason`이 cap/kill/arming/staleness/prior-unsettled/no-fill-window 닫힌 목록이거나 6790 `reserved_waiting`/`skipped_waiting`. `unfilled` — `accepted` submit/amend child가 있고 `execution_ledger_in_effect()`에 체결이 없으며 행이 만료됨. 그 밖(증거 미결·child `sending`/`uncertain`/`shadowed`/비-guard 거부·원장 미독)은 `source_unavailable`이며 다섯 중 하나로 접지 않는다. 매도 행(trim/risk_sell), 보유 행(hold)과 매수가 이미 체결된 행은 다섯 중 어디에도 속하지 않으므로 `source_unavailable`이다. 🔴 증거는 symbol이 아니라 **행 revision**에 결속한다: intent는 그 행의 `plan_version`이 그 row key의 최신 실행변경 seq(`daily_plan.row_lock.latest_change_seq`) 이상이고 side가 행과 같을 때만 쓰며, superseded revision의 child는 새 revision의 사유를 결정하지 않는다. fill은 `broker`·`account_mode`·`venue`·`symbol`·`side`·`currency`·정규화 주문번호·KST 주문일(`trade_day_kst`)의 E1 identity로만 대조하고 accepted amend의 대체 주문번호도 포함한다(원시 주문번호 단독 대조 금지). 6790 비교는 그 행을 게시한 버전의 것을 쓴다 — 다른 행의 이후 게시가 비교를 재작성해도 기존 행의 사유를 바꾸지 않는다.
- **읽기 실패**: head/버전/행 읽기 실패·상한(5,000행)은 그날 계획 증거 전체를 unknown으로 만들고 `source.plan`(`upstream_error`/`unavailable`/`incomplete_evidence`)을 남기며, 계획 행이 모집단에 종목을 더하므로 `total`도 null(`count_unavailable`)이 된다. 번들만 실패하면 행이 없는 종목만 `candidate_not_captured`를 증명할 수 없다. intent/child 읽기 실패는 child 증거를, 원장 읽기 실패는 `unfilled` 판정을 unknown으로 만든다.
- **테스트**: `tests/services/retro_v1/test_retro_timeline_plan.py`(사유마다 fixture 하나 + `source_unavailable` fixture, 순수 `derive_plan_reason` 표, guard refusal 목록 핀), `test_guards.py`(새 모듈도 import closure·SELECT 전용 스캔 대상), 예제 `contracts/retro/v1/examples/retro_timeline.yaml`.

### retro/v1 세션 관측 상태 `session_observation` (hk 6421)

"지금 어느 장을 볼 수 있나"에 저장된 자료만으로 답하는 읽기 operation 이다. 호환 확장이다 — 새 경로(`/trading/api/retro/v1/session_observation`)와 새 스키마만 더하고 기존 경로·필드·열거는 그대로이며 `consumers.lock` 도 그대로다(소비자 게이트 통과 확인, 핀 갱신은 호환 확장 → 소비자 커밋 → 핀 갱신 순서). 🔴 읽기 전용이다: `app/services/retro_v1/session_observation.py` 가 다른 reader 와 같은 `REPEATABLE READ READ ONLY` 스냅샷(8초 timeout) 안에서 SELECT 만 한다. 브로커·거래소 호출, MCP 도구, writer, 스케줄러, 마이그레이션은 없고 시세가 보인다는 것은 어떤 주문 권한도 열지 않는다. Factortrail 도구는 별도 저장소의 후속이다.

- **쿼리**: `date` 필수(시장의 세션 날짜 — KR 은 KST 일, US 는 ET 거래일, 선물은 거래일). `market`(kr|us|futures, 생략 시 셋 다), `symbols`(쉼표 구분 최대 50, 기본 모집단을 대체하며 `movers` 와 함께 쓸 수 없다), `movers`(시장당 0–50, 기본 10), `max_lag_seconds`(60–3600, 기본 600), `limit`·`cursor`.
- **기본 모집단**: 고정 목록 KR `005930,000660,278470` · US `SPY,QQQ,SMH` + 그 날짜 `invest_momentum_event_snapshots` 의 `excluded` 가 아닌 종목(최고 순위 → 최초 포착 → 심볼) 중 고정 목록 밖 `movers` 개. 선물은 `research.kr_futures_candles_1m` 에 최근 7일 저장된 결제월 중 결제월 둘째 목요일이 지나지 않은 것이다(휴장 보정 없음). 후보가 없거나 선물 계약이 없으면 `population.movers_<m>`·`population.futures` 의 `missing_data` unknown 이다.
- **행**: 종목 × venue × 세션. KR 은 KRX(`KRX_REGULAR`·`KRX_AFTER`)와 NTX(`NXT_PRE`·`NXT_OVERLAP`·`NXT_POST`), US 는 거래소(`US_PRE`·`US_REGULAR`·`US_POST`)와 `TOSS_INTEGRATED` `US_DAY`, 선물은 `FUT_DAY`·`FUT_NIGHT` 다. US venue 는 저장된 봉의 거래소가 우선이고 없으면 `us_symbol_universe` 의 활성 행이다(그것도 없으면 `venue=null`). 저장된 다른 venue × 세션 조합은 행으로 더한다. `UNKNOWN` 표지 분은 어느 세션에도 들지 않는다. 필드: `source`(마지막 체결 봉의 저장 출처, 없으면 최신 봉)·`sources`·`stored_bar_count`·`trade_bar_count`·`last_bar_at`·`last_trade_bar_at`(둘 다 봉 **시작**)·`last_received_at`·`receive_lag_seconds`(최신 봉의 최초 수신 − 봉 마감, 올림)·`reference_close_*`·`phase`(`before_open|open|ended|not_trading_day|unknown`)·`state`·`reason`(판정 규칙).
- **봉**: research 1분봉 표만 읽고 `batch_id` 가 `backfill`/`promote-` 인 행은 최초 수신이 아니므로 읽지 않는다(그것만 있는 세션은 `not_collected`). KR 은 hk 6757 단일가 정정의 거래량·출처를 적용한다. 🔴 거래량 0 봉은 저장 사실일 뿐 체결이 아니다. 예상체결가는 읽지 않는다.
- **상태와 규칙(위에서부터 첫 번째)**: 봉 읽기 실패·상한 → `null`/`source_unavailable`(`not_collected` 가 아니다). 봉이 있으면 — `receive_lag_over_threshold`(`delayed`) → `latest_bar_older_than_threshold`(`delayed`, `min(as_of, 세션 끝) − 최신 봉 마감 > max_lag_seconds`; 세션 시각을 모르는 `US_DAY` 에는 적용하지 않는다) → `only_zero_volume_bars`(`no_trades`) → `trade_bars_received`(`observed`). 봉이 없으면 — `not_trading_day`(`closed`) → `calendar_unknown`(null) → `symbol_not_nxt_eligible`(`closed`) → `session_not_started`(`closed`) → `source_unavailable`/`eligibility_unknown`(null) → `collection_unsupported`(`not_collected`, 선물 야간 — hk 6148) → `window_unknown`(null, `US_DAY`) → `awaiting_first_receipt`(null, 시작 + 1분 + `max_lag_seconds` 전) → `symbol_not_in_universe`·`no_stored_bars`(`not_collected`).
- **`observed` 는 다섯 번째 상태**: 요청된 네 상태(`closed`·`no_trades`·`not_collected`·`delayed`)는 볼 수 없는 이유이고, 체결 봉이 제때 저장된 세션에는 `observed` 가 필요하다. 끝난 세션은 `closed` 가 아니라 관측 결과를 유지하며 장이 끝났다는 사실은 `phase=ended` 다.
- **지연 기본값 600초**: 분봉은 5분 간격 수집기(실행 상한 4분)가 쌓으므로 두 주기를 넘는 나이를 지연으로 본다. 더 엄격한 소비자는 `max_lag_seconds` 를 낮추고 원값(`receive_lag_seconds`)을 쓴다.
- **세션 시각**: `session_windows.py` 는 `session_segment_of` 의 창을 옮긴 사본이며(retro import 가드가 원본 import 를 막는다) 테스트가 분 단위로 일치를 고정한다. 거래일·US 조기 폐장은 `market_events.session_calendar`, 분류 불가 날짜는 추측하지 않는다. 두 창만 저장 표지보다 좁다(hk 7002): 선물 주간장은 계약의 최종거래일(둘째 목요일, 휴장이면 앞당김 — `kis_futures_mock.session_calendar` 규칙의 사본이고 일치 테스트가 두 규칙을 맞댄다)에 15:20 에 끝나고, 미국 애프터장은 조기 폐장일(정규장 13:00 ET)에 17:00 ET 에 끝난다(Nasdaq Equity Trader Alert #2026-69 와 NYSE Holidays & Trading Hours 페이지가 그날 확장 세션 종료를 5:00 p.m. ET 로 적는다). 저장 `session_segment` 표지는 바뀌지 않으므로 조기 폐장일 17:00–20:00 ET 봉은 여전히 `US_POST` 다. 최종거래일을 달력이 확인하지 못하면 15:45 를 유지한다 — 창을 줄이지 않는 쪽은 지연을 숨기지 않는다.
- **기준 종가**: 직전 거래일의 종가다(`reference_close_date`; 애프터장에도 당일 정규장 종가가 아니다). KR 은 `krx_official_daily` 에 값이 있으면 `krx_official`, 없으면 `kr_candles_1d` KRX 행(`stored_daily`), US 는 `us_candles_1d` 의 그 날짜 행이 정확히 하나일 때 `stored_daily` 다. 공식 표를 읽지 못하면 일봉으로 내려가지 않는다. 선물은 `unsupported`.
- **알려진 한계**: ① 지연 규칙은 '체결이 없어 봉이 없음'과 '받지 못함'을 구별하지 못한다 — 공급자가 무체결 분을 생략하는 얇은 종목은 `delayed` 로 보일 수 있다. ② `US_DAY` 는 저장된 달력이 없어 봉이 없으면 판정하지 않고, 끊긴 수집도 최신 봉의 수신 지연 밖에는 드러나지 않는다. ③ NXT 적격(`kr_symbol_universe.nxt_eligible`)은 현재 값이다. KRX 애프터마켓 적격은 반영하지 않아 비적격 종목의 `KRX_AFTER` 는 `not_collected` 로 보인다. ④ 수능일 같은 특별 시간은 저장 표지와 같이 반영하지 않는다. ⑤ 선물 최종거래일 단축은 계약 심볼이 있어야 적용된다 — 심볼 없이 부르는 호출(테스트·도구)은 15:45 창을 받는다. ⑥ 기본 모집단의 계약 만료 판정(`contract_expired`)은 휴장 보정 없는 둘째 목요일이므로, 둘째 목요일이 휴장인 달에는 만료된 계약이 그날 모집단에 남는다(창은 이미 최종거래일에서 15:20 에 끝난다). ⑦ 미국 조기 폐장은 정규장 종료가 16:00 ET 보다 이른지로만 판정하므로, 달력이 조기 폐장을 모델링하지 못하는 날은 20:00 ET 로 남는다.
- **테스트·예제**: `tests/services/retro_v1/test_session_observation{,_db}.py`(DB 는 CI), `test_guards.py`, `tests/routers/test_retro_v1_http.py`, `contracts/retro/v1/examples/session_observation.yaml`.

### KR 수급·이벤트 읽기 도구 (hk 6664)

`get_kr_flow_signals(symbol, days=20)`와 `get_kr_investor_rankings(investor="foreign", side="buy", limit=20)`는 DEFAULT 프로필에서만 등록되는 읽기 전용 MCP 도구다. `config/mcp_lane_allowlists/kr.txt`만 두 도구를 광고한다(`spec:kr-flow-signals`). DB·주문·원장 쓰기와 스케줄러 연결은 없다.

- `get_kr_flow_signals` 결과는 `risk`와 `reference` 두 블록이다. `risk`는 KIS VI와 기존 `kr_symbol_trading_flags` 저장소의 거래정지·관리·시장경고·정리매매 상태를 반환한다. 저장 행이 없는 분류는 `state="unknown"`으로 두며 `none`으로 추론하지 않는다. `reference`는 Toss 공매도·대차·신용·프로그램매매·투자자 랭킹과 KIS 회원사 매수/매도 상위 정보를 각각 독립 블록으로 반환한다. 각 소스는 `status`(`ok`/`empty`/`error`), 제공자 `as_of`, 로컬 `fetched_at`, `data` 또는 `error_class`를 가진다. 제공자 시각이 없으면 `as_of`는 null이다. 한 소스의 실패는 다른 소스를 버리지 않는다.
- `days`는 1..100의 정확한 정수이며 Toss endpoint의 `count`로 전달된다. KIS VI는 호출 당일 KST의 단일 날짜를 읽고, 회원사 매매동향은 현재 요약이다.
- `get_kr_investor_rankings`는 `investor ∈ {foreign, institution, individual}`, `side ∈ {buy, sell}`, `limit` 1..100을 받으며 `reference.toss_investor_rankings` 블록으로 반환한다. `as_of`는 해당 투자자 섹션의 제공자 `rankedAt`이며 null일 수 있다. 금액은 KRW 정수 단위로 온다.
- 공식 근거: [Toss Open API v1.2.28](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json), [KIS 국내주식 공식 샘플](https://github.com/koreainvestment/open-trading-api/blob/main/examples_user/domestic_stock/domestic_stock_functions.py).
