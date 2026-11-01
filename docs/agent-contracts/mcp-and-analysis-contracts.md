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
live 프롬프트↔서빙 표면 계약(#1003, ROB-1436 이후 DEFAULT): `tests/mcp_server/test_live_prompt_profile_contract.py` + 핀 `tests/fixtures/live_prompt_tool_requirements.yaml` — live 프롬프트가 served/required 로 기록한 도구가 DEFAULT(게이트 on)에 등록돼 있지 않으면 실패한다(strict, xfail 없음). 도구 정리로 프롬프트 필수 도구를 등록 해제하면 이 테스트가 막는다. 절차: 운영자 레포 프롬프트가 바뀌면 핀을 새 operator commit 으로 재고정.

**MCP 유닛 은퇴 (ROB-1436, 2026-11-01, 운영자 결정)**: NCP 고정 유닛 `at-mcp-analysis-readonly`(8768)·
`at-mcp-account-read`(8769)·`at-mcp-tradingcodex-execution`(8770)·`at-mcp-paper-001`(8771)·
`at-mcp-live-kr/-us/-crypto`(8773-8775)를 `scripts/deploy-ncp-pull.sh`·HAProxy 템플릿·`docker-compose.prod.yml` 에서
제거했다. 남은 고정 유닛은 kiwoom·h3-crypto-paper·h3-us-paper 뿐이다(근거·소비자 표·정지 순서·롤백은
`docs/runbooks/mcp-unit-retirement-rob1436.md`). 프로필 `analysis_readonly`·`account_read`·`tradingcodex_execution`·
`live-kr`·`live-us`·`live-crypto` 와 그 registrar·`config/mcp_profiles/live.yaml`·fable-workbench 레인 allowlist 는 삭제됐고
이 이름의 `MCP_PROFILE` 은 미지 프로필로 기동 거부된다. 🔴 이제 레포에 **물리적 읽기 전용 MCP 표면이 없다** — 외부 읽기 세션이
필요해지면 새 리뷰로 추가한다(운영자 결정). DEFAULT 는 주문 도구를 등록하므로 읽기 전용 대체물이 아니다.
live 세션은 prefect `KR_LIVE_MCP_MODE=shared`(기본)로 DEFAULT 를 쓴다 — 🔴 `dedicated` 로 바꾸면 사라진 8773-8775 에 붙으려다
실패하므로 prefect 쪽 dedicated 분기 제거는 별도 후속이다. `hermes-paper-kis` 프로필은 유닛만 은퇴하고 코드는 남는다(레인
claude-mock/orch-mock 계약, 운영자 H3-KR 선언, Task 881 도구). 은퇴한 포트는 새 리뷰 없이 재사용 금지.
**저트래픽 프로필 지연 import (ROB-1436)**: `app/mcp_server/lazy_imports.py` — `kiwoom`·`kiwoom_kr`·`h3-crypto-paper`·
`h3-us-paper` 프로세스는 `main.py` 가 tooling import 전에 pandas·numpy·yfinance·exchange_calendars·
pandas_market_calendars·finnhub 를 placeholder 모듈로 등록해 첫 속성 접근(첫 사용 도구 호출) 때 실제 import 한다.
DEFAULT·그 밖 프로필은 eager 그대로다. 도구 출력 불변. 🔴 모듈 최상위에서 이 패키지들의 속성에 접근하지 마라
(`from pandas import X`, `from __future__ import annotations` 없는 `pd.DataFrame` 어노테이션, `pd.Timestamp(...)` 상수) —
`tests/mcp_server/test_lazy_imports.py` 가 지연 프로필 등록 후 실행된 패키지가 0개인지 검증한다. 측정:
`scripts/mcp_profile_import_rss.py --compare`.
**h3-crypto-paper 전용 프로필 (#1171, 운영자 hk 1135 = A)**: `MCP_PROFILE=h3-crypto-paper` 는
auto_trader-operator H3-CRYPTO 파일럿 러너(`registered_tools("crypto")`)의 20개 도구만 등록하는 closed world다
(`app/mcp_server/tooling/h3_crypto_paper_registration.py`, "Always" 블록 전 early return, 등록 집합 ≠ 목록이면 부팅 실패).
주문 표면은 ROB-703 paper 시뮬레이터 4종뿐이고 live 주문·live 계좌 조회·proposal/watch/설정/정책 변경 도구는 없다.
`get_holdings` 는 이 프로필에서 DB paper 계좌(`paper`/`paper:<이름>`, `account_mode=db_simulated`)로 고정된다.
브로커 자격증명에 닿을 수 있는 목록 도구 11종은 `market="crypto"` 로 고정되고(생략 시 crypto, 그 밖은 본문 실행 전 거부),
`get_operating_briefing` 은 `account_scope="db_simulated"`(DB paper 보유, 브로커 미체결 수집기 미생성)로 고정된다.
네트워크 전송은 토큰 없이 부팅하지 않는다. 유닛(`at-mcp-h3-crypto-paper`, 루프백 8776, 토큰 `MCP_H3_CRYPTO_PAPER_AUTH_TOKEN`)은
#1189부터 `scripts/deploy-ncp-pull.sh` 가 다른 고정 유닛과 같은 digest 고정·교체 로그·롤백·tailnet 라우트 probe·#934 prune 규칙으로
배포하고, HAProxy 는 tailnet `100.122.100.56:8776` 만 바인드한다. 스케줄러 연결은 없다. 토큰 생성·배포는 운영자 몫이다
(`docs/runbooks/h3-crypto-paper-mcp.md`). 계약 테스트: `tests/mcp_server/test_h3_crypto_paper_profile.py`,
`tests/scripts/test_deploy_ncp_pull_h3_crypto_paper.py`.
**h3 paper-execution route 계약 (#1244)**: 이 프로필에는 proposal 도구가 설계상 없으므로 `route_request` 는
등록 시점에 명시한 `execution_surface="paper_simulator"`(h3 registrar 한 곳만 지정, 도구 집합으로 추론하지 않음)로
crypto buy/sell(`buy_analysis`/`profit_taking`)에 `paper-execution-v1` 계약을 낸다 — 실행 도구는
`paper_place_limit_order`/`paper_cancel_pending_order` 두 개뿐이고(#1244 r3: h3-us-paper 는 `execution_surface="alpaca_paper"` 로
us buy/sell 에 같은 계약 — 실행 도구 `alpaca_paper_submit_order`/`alpaca_paper_cancel_order`, 필수 읽기
`alpaca_paper_list_orders`/`alpaca_paper_get_order`/`alpaca_paper_list_positions`/`market_quote_snapshot_ensure`), 같은 표면에 proposal·live·mock 주문 도구가 하나라도
등록되면 degraded + `foreign_execution_tools` 로 fail-closed 하며 그 도구를 허용·시퀀스에 넣지 않는다. paper 가 아닌 reconcile writer(live/mock 원장)도 paper route 에서는 허용·시퀀스에서 숨긴다(degrade 는 아님).
paper route 는 브로커 전용 제약 "accepted/resting is not a fill; broker evidence reconcile is required" 를 자기 paper 계좌의
주문 읽기로 체결을 확인하는 문구로 바꾼다(proposal route 는 그대로). 그 밖의 프로필·intent·
market·purpose 의 출력은 main 과 바이트 동일하다(`tests/mcp_server/test_route_request_profile_golden.py`,
`test_route_request_paper_surface.py`).
**h3-us-paper 전용 프로필 (#1257, #1245)**: `MCP_PROFILE=h3-us-paper` 는 H3-US 파일럿 러너(`registered_tools("us")`)의
20개 도구만 등록하는 closed world다(`app/mcp_server/tooling/h3_us_paper_registration.py`, us-paper 의 strict subset).
주문 표면은 수동 Alpaca paper submit/cancel 2종뿐이고 automated submit·preview·reconcile writer·live 주문·live 계좌 조회·
proposal/watch/설정 도구는 없다. 시장 인자 도구 10종은 `market="us"`, Alpaca 5종은 `account_mode="alpaca_paper"`
(lab/crypto 거부), submit 은 `asset_class="us_equity"`, `get_operating_briefing` 은 `account_scope="db_simulated"` 로 고정된다.
🔴 모든 도구 본문은 `app/services/brokers/credential_firewall.broker_credentials_blocked` 안에서 실행되어 KIS·Toss·Upbit 인증
클라이언트는 토큰 조회·breaker lease·전송 전에 `BrokerCredentialsBlocked` 로 거부된다(US 시세는 Yahoo, 일봉은 캐시 행,
USD/KRW 는 open.er-api 로 기존 폴백). 이 방화벽 검사를 KIS/Toss/Upbit 클라이언트에서 제거하지 마라.
유닛 `at-mcp-h3-us-paper`(루프백 8777, 토큰 `MCP_H3_US_PAPER_AUTH_TOKEN`, tailnet `100.122.100.56:8777`)는 h3-crypto-paper 와
같은 규칙으로 배포된다. buy_analysis/profit_taking(us) route 는 #1244 r3 부터 Alpaca paper-execution 계약으로 degraded 가 아니다
(`docs/runbooks/h3-us-paper-mcp.md`). 계약 테스트: `tests/mcp_server/test_h3_us_paper_profile.py`,
`tests/scripts/test_deploy_ncp_pull_h3_us_paper.py`, `tests/services/brokers/test_credential_firewall.py`.
**route_request kr/us 시장 보충 허용 (hk 1040, 운영자 10-16 dr-1040-1 A)**: `route_request_lanes.MARKET_EXTRA_ALLOWED`
= kr·us 에 `toss_get_orderable_cash` 하나. robin-prefect 가 KR/US live rep argv 에만 이 도구를 싣는다(`LIVE_KR_US_ONLY_TOOLS`).
그래서 kr/us route 는 모든 lane 에서 이 도구를 `allowed_tools` 에 두고 `blocked_actions` 에서 뺀다(시퀀스 step 아님, 레지스트리 교집합 유지).
crypto route 는 계속 차단한다. 레지스트리 분할(`STATUS_HELPER_TOOLS`·`MUTATION_TOOLS`)은 그대로이고, 브로커 호출은 Toss `GET /api/v1/buying-power`(+`/accounts`)뿐이다.
`test_route_request_profile_golden.py` 는 이 한 도구 이동을 되돌린 바이트가 main golden 과 같음을 검증한다
(default 의 kr/us 케이스만 이동; tradingcodex_execution·live-kr/live-us 는 ROB-1436 에서 은퇴).
**MCP_PROFILE 필수 (#1189)**: 서버 진입점은 `require_mcp_profile` 로 프로필을 해석하며 비었거나·없거나·공백뿐인
`MCP_PROFILE` 은 DEFAULT 로 폴백하지 않고 기동을 거부한다(DEFAULT 는 `MCP_PROFILE=default` 로 명시). deploy 스크립트도
배포 전 모든 유닛의 프로필이 비어 있지 않음을 검사한다. 레포 안 모든 서버 정의의 명시 프로필은
`tests/mcp_server/test_mcp_profile_required.py` 가 목록으로 검증한다. 이 폴백을 되살리지 마라.
Task 881의 `kis_mock_ledger_expire_day_orders`는 별도 registrar로
hermes-paper-kis에만 등록한다. default/live 및 다른 모든 profile에는 없으며
스냅샷과 과거 감사 예외 테스트가 이 물리적 경계를 검증한다. DB-only 도구여도
KIS_MOCK_ENABLED 및 기존 필수 설정 게이트, dry_run 기본 true와 쓰기 confirm
true를 유지한다.

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

`investment_watch_create` additionally takes a **top-level** `action_mode`
(`notify_only`/`approval_required` only — `preview_only`/`auto_execute_mock`
are nested-`watch_condition`-only). Top-level and an explicitly-sent
`watch_condition.action_mode` must agree; a mismatch is rejected with
`error="action_mode_conflict"` rather than silently coerced. An effective
`approval_required` also requires non-empty `max_action`
(`error="max_action_required"`, `required_fields=["max_action"]`). Both
rejections are single-response dicts on the MCP create path; existing rows,
the scanner, and reprocessing paths are unaffected.

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


## 유지 규약

새 기능·안전 경계·계약을 추가하거나 변경할 때에는 관련 분야 계약을 같은 PR에서 갱신하고, 필요하면 두 진입점의 최소 하드룰과 명시적 필독 목록도 함께 갱신한다.
