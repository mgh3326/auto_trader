# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

AI 기반 자동 거래 분석 시스템으로, 다양한 금융 데이터를 수집하고 Google Gemini AI를 활용하여 투자 분석을 제공합니다.

**주요 특징:**
- 다중 시장 지원: 국내주식(KIS), 해외주식(KIS/Yahoo Finance), 암호화폐(Upbit)
- 다중 시간대 분석: 일봉 200개 + 분봉(60분/5분/1분)
- AI 분석: Google Gemini API를 통한 구조화된 JSON 분석
- Redis 기반 API 키별 모델 제한 시스템

## 개발 환경 설정

### 필수 요구사항
- Python 3.13+
- UV (의존성 관리)
- PostgreSQL (데이터베이스)
- Redis (모델 제한 관리 및 캐싱)

### 초기 설정
```bash
# UV 설치 (미설치 시)
pip install uv

# 의존성 설치
uv sync                           # 프로덕션 의존성만
uv sync --all-groups              # 모든 의존성 (test, dev 포함)

# 환경 변수 설정
cp env.example .env
# .env 파일 편집하여 API 키 설정

# 데이터베이스 마이그레이션
uv run alembic upgrade head

# 개발 서버 실행
make dev                          # 또는 uv run uvicorn app.main:app --reload
```

### Docker 환경
```bash
docker compose up -d              # PostgreSQL, Redis, Adminer 시작
docker compose ps                 # 서비스 상태 확인
docker compose down               # 서비스 중지
```

## 핵심 명령어

### 테스트
```bash
make test                         # 모든 테스트 실행
make test-unit                    # 단위 테스트만
make test-integration             # 통합 테스트만
make test-cov                     # 커버리지 리포트 포함
uv run pytest tests/test_*.py -v -k "test_name"  # 특정 테스트만
```

### 코드 품질
```bash
make lint                         # Ruff + ty 검사
make format                       # Ruff로 코드 포맷팅
make typecheck                    # ty 타입 체킹
make security                     # bandit, safety 보안 검사
```

### 데이터베이스
```bash
# 마이그레이션 생성 및 적용
uv run alembic revision --autogenerate -m "migration message"
uv run alembic upgrade head

# 마이그레이션 롤백
uv run alembic downgrade -1

# 현재 버전 확인
uv run alembic current
```

### 개발 도구
```bash
python manage_users.py list                 # 사용자 권한/상태 확인
python websocket_monitor.py --mode both     # 통합 WebSocket 모니터링
python kis_websocket_monitor.py             # KIS WebSocket 모니터링
python upbit_websocket_monitor.py           # Upbit WebSocket 모니터링
```

## 아키텍처

### 분석 시스템 아키텍처

**핵심 설계 원칙: 공통 로직 분리 + 서비스별 특화**

```
app/analysis/
├── analyzer.py              # 핵심 Analyzer 클래스 (공통 로직)
│   ├── Analyzer             # 프롬프트 생성, AI 호출, DB 저장, 재시도 로직
│   └── DataProcessor        # 데이터 전처리 유틸리티
└── service_analyzers.py     # 서비스별 분석기 (상속)
    ├── UpbitAnalyzer        # 암호화폐 (분봉 지원)
    ├── YahooAnalyzer        # 미국 주식 (분봉 미지원)
    └── KISAnalyzer          # 국내/해외 주식 (분봉 지원)
```

**각 서비스 분석기는:**
- `Analyzer`를 상속하여 공통 기능 재사용
- 데이터 수집 로직만 서비스별로 구현 (`_collect_*_data` 메서드)
- 보유 자산 정보가 있으면 `position_info`로 전달
- 분봉 데이터가 있으면 `minute_candles`로 전달

### Runtime LLM ownership boundary

auto_trader runtime code must not import or instantiate in-process LLM providers
(Gemini/OpenAI/Grok/etc.). LLM judgment belongs to MCP consumers or Hermes
out-of-process flows. The static guard in
`tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py`
scans `app/**/*.py` for forbidden provider imports and deleted provider files.

### Investment Report Item Contract

`investment_report_create` / `investment_report_add_items` reject unknown top-level item keys. Use typed fields for current contracts:

- `trigger_checklist`: `string[]`; copied to watch trigger notifications.
- `max_action`: structured execution-plan JSON for watch items. `account_mode` is required when `max_action` is present; it also requires `side` and exactly one of `quantity` or `notional`; optional keys include `amount_krw`, `limit_price`, `limit_price_hint`, and `ladder_level`.
- Do not send `planned_action` as an item key. Hermes payloads derive `planned_action` from `max_action`.

### Alpaca Paper 실행 레저 (ROB-84)

`review.alpaca_paper_order_ledger` — Alpaca Paper 주문 라이프사이클 레코드 (previewed → canceled/filled/unexpected).

- **ORM 모델**: `app/models/review.AlpacaPaperOrderLedger`
- **서비스**: `app/services/alpaca_paper_ledger_service.AlpacaPaperLedgerService` — 모든 쓰기는 이 서비스를 통해서만 허용
- **라우터**: `app/routers/alpaca_paper_ledger.py` — GET 전용 (`/trading/api/alpaca-paper/ledger/...`)
- **MCP 도구**: `alpaca_paper_ledger_list_recent`, `alpaca_paper_ledger_get` (read-only)
- **런북**: `docs/runbooks/alpaca-paper-ledger.md`

**주의**: 서비스는 브로커 mutation 없음. 직접 SQL INSERT/UPDATE/DELETE 금지.

### Binance Demo Order Ledger (ROB-298)

`binance_demo_order_ledger` — unified Demo execution lifecycle ledger. Keyed by `product` discriminator (`spot` in PR 1; `usdm_futures` reserved for PR 2). All writes via service layer.

- **ORM 모델**: `app/models/binance_demo_order_ledger.BinanceDemoOrderLedger`
- **서비스**: `app/services/brokers/binance/demo/ledger/service.BinanceDemoLedgerService` — 모든 쓰기는 이 서비스를 통해서만 (8개 `record_*` 메서드)
- **리포지토리**: `app/services/brokers/binance/demo/ledger/repository.BinanceDemoLedgerRepository` — 서비스 내부 전용 (AST guard로 외부 import 금지)
- **상태 머신**: `BinanceDemoInvalidStateTransition` — `planned → previewed → validated → submitted → filled → closed → reconciled` + `cancelled`/`anomaly` branches
- **Spot 실행 어댑터**: `app/services/brokers/binance/spot_demo/execution_client.BinanceSpotDemoExecutionClient` — `demo-api.binance.com` only; mutation은 `submit_order(..., confirm=True)` 만
- **CLI**: `scripts/binance_spot_demo_smoke.py` (default-disabled, 5 modes)
- **런북**: `docs/runbooks/binance-spot-demo-smoke.md`

**안전 경계**:
- **Demo 전용 호스트**: Spot Demo는 `demo-api.binance.com`만 허용 (`assert_spot_demo_host`); live/mainnet/testnet host는 transport 레이어에서 fail-closed (`_DEPRECATED_TESTNET_HOSTS` deny-list 유지)
- **Default-disabled**: `BINANCE_SPOT_DEMO_ENABLED=true` 미설정 시 `BinanceSpotDemoDisabled`
- **Per-call operator gate**: `submit_order(..., confirm=True)` 매 호출마다 명시되어야 실 HTTP 발생; default는 `SpotDemoDryRunResult`
- **TESTNET env vars do nothing**: `BINANCE_TESTNET_*`는 Demo trading을 활성화 못함 (테스트로 증명)
- **Sizing**: LOT_SIZE.stepSize floor, MIN_NOTIONAL guard, round-up 금지 — cap 초과면 blocked
- **선물 path**: PR 2에서 별도 `futures_demo/` backend로 추가 (아래 참고)
- **스케줄러 활성화 없음**: TaskIQ/cron/Prefect 연결 없음. CLI에서만 호출
- **프로덕션 cutover gate**: alembic 마이그레이션은 PR에 포함되지만 operator가 별도로 `alembic upgrade head` 실행

**USD-M Futures Demo (ROB-298 PR 2)**:
- **실행 어댑터**: `app/services/brokers/binance/futures_demo/execution_client.BinanceFuturesDemoExecutionClient` — `demo-fapi.binance.com` only; mutation은 `submit_order(..., confirm=True)`만; close 주문에는 `reduce_only=True` 필수
- **호스트 분리**: `FUTURES_DEMO_HOSTS = {demo-fapi.binance.com}`, Spot Demo (`demo-api.binance.com`)와 disjoint; live/testnet futures (`fapi.binance.com`, `testnet.binancefuture.com`) 차단
- **env namespace**: `BINANCE_FUTURES_DEMO_*` 전용 (Spot Demo와 비공유)
- **Leverage**: `1x` 강제 (`set_leverage` echo로 검증; mismatch → `BinanceFuturesDemoLeverageMismatch`)
- **Position mode**: One-way only (Hedge → `BinanceFuturesDemoHedgeModeBlocked`)
- **Symbol allowlist**: `XRPUSDT` (default), `DOGEUSDT`, `SOLUSDT` (fallback). `BTCUSDT` 제외 (MIN_NOTIONAL=50 > cap=10). operator `--allow-symbol` override 시도해도 excluded list 우선
- **Reconcile gate**: 클로즈 후 open orders empty AND position flat 둘 다 만족해야 `reconciled`. 둘 중 하나라도 dirty면 `anomaly` 기록
- **`status=NEW` reconcile (ROB-305 §4)**: MARKET submit이 `NEW`를 반환해도 즉시 성공/실패로 단정하지 않음. `submitted → closed` 직행 금지(상태머신이 차단). fill 증거는 submit status → bounded `GET /fapi/v1/order` poll(`_FILL_RECONCILE_MAX_POLLS`, 무한 루프 없음) → non-flat positionRisk 순으로 확인 후에만 `filled` 기록. fill 증명 불가인데 account가 flat + open orders 0이면 close row를 `anomaly`로 기록하고 exit 2 (clean success로 위장 금지). 단일주문 status 조회는 `BinanceFuturesDemoExecutionClient.get_order`
- **CLI**: `scripts/binance_futures_demo_smoke.py` (default-disabled, 5 modes)
- **런북**: `docs/runbooks/binance-futures-demo-smoke.md`

### KIS WebSocket Mock Smoke (ROB-104)

`scripts/kis_websocket_mock_smoke.py` — KIS 모의 WebSocket 핸드셰이크 검증 (주문/체결/Redis publish 없음).

- **CLI**: `uv run python -m scripts.kis_websocket_mock_smoke`
- **런북**: `docs/runbooks/kis-websocket-mock-smoke.md`
- **이벤트 태깅**: `app/services/kis_websocket_internal/events.py::build_lifecycle_event` (ROB-100 `OrderLifecycleEvent`)

### KIS Live Order Fill-Evidence Gate (ROB-395)

`kis_live_place_order(dry_run=False)` (KR domestic) records **accepted-only** to
`review.kis_live_order_ledger` — no fill/journal/realized_pnl at send. Fills are
booked only by `kis_live_reconcile_orders` from order-id-keyed
`inquire_daily_order_domestic` evidence (reuses `classify_fill_evidence`).

- **모델**: `app/models/review.KISLiveOrderLedger`
- **서비스**: `app/mcp_server/tooling/kis_live_ledger.py`
- **MCP 도구**: `kis_live_reconcile_orders` (dry_run-default)
- **런북**: `docs/runbooks/kis-live-order-reconcile.md`
- **스코프**: KR live only; US/crypto live unchanged (follow-up)

### US & Crypto Live Order Fill-Evidence Gate (ROB-407)
...
시장가 crypto 주문의 경우 전송 즉시 inline으로 Reconcile을 자동 수행하여 체결 장부를 확정합니다.

- **모델**: `app/models/review.LiveOrderLedger`
- **서비스**: `app/mcp_server/tooling/live_order_ledger.py`, `app/mcp_server/tooling/live_order_evidence.py`
- **MCP 도구**: `live_reconcile_orders` (dry_run-default)
- **런북**: `docs/runbooks/live-order-reconcile.md`
- **스코프**: US/해외 및 crypto live 주문 전체.

### KR/US Category Normalization & Lazy Fill (ROB-512)

KR Naver 업종과 US Yahoo Finance Industry/Sector를 `symbol_sectors` 테이블로 통합 관리합니다.

- **마스터 모델**: `app/models/symbol_sectors.SymbolSector` (`source_key`가 식별자)
- **Lazy Fill**: 스크리너 조회 시(enrichment) 섹터가 없는 종목은 실시간 fetch 후 DB에 저장합니다.
- **서비스**: `app/services/symbol_sectors_service.py` (쓰기 전용), `app/services/us_sector_korean_map.py` (US 한글 매핑)
- **적용 로더**: `investor_flow`, `consecutive_gainers`, `double_buy`, `fundamentals` 등 주요 스크리너 로더에 JOIN 배선 완료.
- **표시 규칙**: `SymbolSector.name_kr` ?? `SymbolSector.name_en` ?? "-" (US는 한글 매핑 우선).

### Kiwoom Mock Account Lifecycle (ROB-97 / ROB-319)

Kiwoom **모의투자** 전용 MCP order/account lifecycle. 7개 도구 모두 `account_mode="kiwoom_mock"`, KRX only.

- **MCP 도구**: `app/mcp_server/tooling/orders_kiwoom_variants.py` — `kiwoom_mock_preview_order`, `kiwoom_mock_place_order`, `kiwoom_mock_modify_order`, `kiwoom_mock_cancel_order`, `kiwoom_mock_get_order_history`, `kiwoom_mock_get_positions`, `kiwoom_mock_get_orderable_cash`
- **클라이언트**: `app/services/brokers/kiwoom/` — `client.KiwoomMockClient` (transport, host allowlist), `domestic_orders.KiwoomDomesticOrderClient` (buy/sell/modify/cancel), `domestic_account.KiwoomDomesticAccountClient` (orderable-amount/balance/order-status/order-detail)
- **스모크 CLI**: `scripts/kiwoom_mock_smoke.py` (default-disabled, 3 modes: preflight/preview/full)
- **런북**: `docs/runbooks/kiwoom-mock-smoke.md`

**ROB-319에서 완성된 것**:
- account-read 도구(`get_orderable_cash`/`get_positions`/`get_order_history`)는 stub-success가 아니라 `KiwoomDomesticAccountClient` 실 호출 결과를 반환. `success`는 broker `return_code`에서 파생(`_derive_broker_success`), raw `broker_response` 첨부.
- `get_orderable_cash`: symbol 있으면 `get_orderable_amount`, 없으면 `get_balance`. cash를 확정 파싱 못하면 `cash: null` + `cash_source: "*_unparsed"` (fake 금지).
- confirmed `modify_order`/`cancel_order`는 `KiwoomDomesticOrderClient`로 연결. modify는 `new_price`+`new_quantity` 둘 다, cancel은 `symbol`+`cancel_quantity` 필수. 비-zero `return_code`는 fake success 아닌 broker-evidence 실패로 표면화.

**안전 경계**:
- **Mock 호스트 only**: `mockapi.kiwoom.com`만 허용 (`KiwoomMockClient` base-URL 거부 + build 후 host 재검증); live `api.kiwoom.com`은 선택 불가 방어 상수
- **Default-disabled**: `KIWOOM_MOCK_ENABLED=true` + `KIWOOM_MOCK_APP_KEY/APP_SECRET/ACCOUNT_NO` 미설정 시 fail-closed
- **`dry_run=False` requires `confirm=True`**: 모든 주문 mutation 도구
- **KRX only**: `NXT`/`SOR`/비-KRX 거부 (네트워크 호출 전)
- **No secrets printed**: CLI는 missing env key **이름만** 보고, 값 출력 없음
- **Cancel-before-submit**: `full` 모드는 cancel이 wired이기에만 실주문 제출; finally-block에서 항상 cancel 시도 후 reconcile

### 토스증권 Open API (ROB-529)

토스증권 Open API(`https://openapi.tossinvest.com`, OAuth2 Client Credentials, REST-only) 기반 KR/US **live** 브로커 + 시세·종목마스터·환율·캘린더 데이터 소스. 모의투자 없음(live 단일).

- **클라이언트**: `app/services/brokers/toss/` — `transport.py`(host allowlist `openapi.tossinvest.com` + **https 강제**, 3xx 거부), `auth.TossOAuthTokenManager`(OAuth, **client당 유효 토큰 1개**라 Redis 공유+단일비행+failed-token double-check, ROB-262 패턴), `rate_limiter`(프로세스 전역 싱글톤 `get_shared_rate_limiter`, 그룹별 per-group lock TPS, 09:00–09:10 ORDER 3TPS), `errors.parse_toss_response`(envelope + non-json typed), `client.TossReadClient`(read + place/modify/cancel)
- **주문 MCP 도구**: `app/mcp_server/tooling/orders_toss_variants.py` — `toss_preview/place/modify/cancel_order`, `toss_get_order_history/positions/orderable_cash` (account_mode `toss_live`). dry_run+confirm 이중 게이트, 손실매도 가드, opposite-pending 사전검사, `clientOrderId` 멱등
- **레저**: `review.toss_live_order_ledger` (`app/services/toss_live_order_ledger_service.py`, accepted-only + `record_send` 멱등 replay) + `toss_reconcile_orders`(단건 상세 fill-evidence, ROB-395/407 패턴)
- **데이터 소스**: 환율 `exchange_rate_service`(토스 primary+폴백, midRate), 종목 마스터+시총 `toss_symbol_master_service`(gap-fill only — 기존 source 있으면 skip), warnings 가드 `warnings_guard`(LIQUIDATION 매수만 차단·매도 면제), 캔들 `market_data/toss_ohlcv`(1m/5m/15m/30m toss-first 페이지네이션, 1h는 DB hourly), 캘린더 `brokers/toss/market_calendar`(NXT/데이마켓)
- **CLI/런북**: `scripts/toss_live_smoke.py`(preflight/order-test/confirm), `docs/runbooks/toss-live-smoke.md`, `toss-live-order-reconcile.md`, `toss-symbol-master-sync.md`
- **ROB-651 (P6-A)**: `toss_preview_order`가 정규화(tick-snap) 이후 `approval_hash`(self-contained 토큰, TTL 5분) + `approval_expires_at`를 반환. `toss_place_order(approval_hash=...)`는 자기 파라미터로 canonical을 재계산해 불일치/만료 시 fail-closed(`error_code` + `diff`). 롤아웃 `TOSS_APPROVAL_HASH_MODE ∈ {off,optional,warn,required}`(기본 `optional`, 백컴팻). `clientOrderId`는 uuid4 → 결정적 `tossp6-<sha16>(canonical|거래일salt|rung)` 멱등키(KR=KST/US=ET 거래일; 같은 거래일 동일주문 dedupe, 익일 신규). 같은 날 진짜 동일 두 번째 주문은 `rung` discriminator로 분리. 컬럼: `review.toss_live_order_ledger.approval_hash`(digest). 공유경로(KIS/Upbit)는 ROB-653 P6-B.
- **ROB-653 (P6-B)**: `place_order` (KIS/Upbit 공통) 및 `kis_live_place_order` 에 `approval_hash` + `rung` 가드를 적용. KIS 주문은 실서버 전송 전 `review.order_send_intents` 테이블에 `idempotency_key`를 선점(reserve)하여 로컬 double-send 중복을 fail-closed로 차단(crypto/Upbit은 Upbit `identifier` 파라미터로 broker-side 멱등 처리). 롤아웃 `ORDER_APPROVAL_HASH_MODE ∈ {off,optional,warn,required}` (기본 `optional`). 컬럼: `review.kis_live_order_ledger` 및 `review.live_order_ledger` 에 `approval_hash` 및 `idempotency_key` 추가.

**안전 경계 / env 게이트 (모두 default off)**:
- `TOSS_API_ENABLED` — 마스터 게이트. 미설정 시 read 클라이언트도 `TossApiDisabled`
- `TOSS_API_CLIENT_ID` / `TOSS_API_CLIENT_SECRET` — 운영 secret(repo commit 금지)
- `TOSS_LIVE_ORDER_MUTATIONS_ENABLED` — 실주문(place/modify/cancel) **및** 보유 routable/orderable/isTradeable 표면(ROB-549)을 함께 arm. live-smoke 클리어 전까지 false
- **KR 주문은 계좌 "투자자지시 거래소 = 통합(SOR)" 설정 필수** (아니면 422 `investor-exchange-not-integrated`)
- ⚠️ `opposite-pending-order-exists`: 동일 종목 반대방향 대기주문 거부 → 매수+매도 래더 동시 거치 불가
- warnings TaskIQ task(`warnings.toss.sync`)는 **scheduleless** 출고(operator/Prefect 등록); disabled 시 graceful skip

### Market Events Ingestion Foundation (ROB-128)

시장 이벤트 (US earnings, KR DART 공시, 향후 crypto/economic) 수집·저장·조회 foundation.

- **모델**: `app/models/market_events.py` — `MarketEvent`, `MarketEventValue`, `MarketEventIngestionPartition`
- **서비스**: `app/services/market_events/` — `repository`, `ingestion`, `query_service`, `normalizers`, `taxonomy`
- **라우터**: `app/routers/market_events.py` — GET `/trading/api/market-events/today`, `/range` (read-only)
- **CLI**: `scripts/ingest_market_events.py` — `--source finnhub|dart --category earnings|disclosure --market us|kr --from-date --to-date [--dry-run]`
- **런북**: `docs/runbooks/market-events-ingestion.md`

**안전 경계**: read-mostly 마켓 데이터, 브로커/주문/감시 mutation 없음. `raw_payload_json` 은 저장 전 `_redact_sensitive_keys` 적용. 모든 DB 쓰기는 `MarketEventsRepository` 경유. Prefect 배포는 후속 작업.

### Research Reports Integration (ROB-140)

브로커 리서치 리포트 (Naver Research / KIS Research 등) `research-reports.v1` 페이로드의 thin ingest/read-layer 통합.

- **모델**: `app/models/research_reports.py` — `ResearchReport`, `ResearchReportIngestionRun`
- **스키마**: `app/schemas/research_reports.py` — `ResearchReportIngestionRequest`, `ResearchReportCitation`, copyright 가드
- **서비스**: `app/services/research_reports/` — `repository`, `ingestion`, `query_service`
- **라우터**: `app/routers/research_reports.py` — GET `/trading/api/research-reports/recent`
- **CLI**: `scripts/ingest_research_reports.py` — `--file path/to/payload.json [--dry-run]`
- **런북**: `docs/runbooks/research-reports-integration.md`

**안전 경계**: 풀 PDF 본문 / 전체 추출 텍스트는 스키마 단계에서 거부 (`full_text_exported`/`pdf_body_excluded=true` 페이로드는 reject). `summary_text` 1000자, `detail.excerpt` 500자로 트렁케이트. 모든 DB 쓰기는 `ResearchReportsRepository` 경유. 브로커/주문/감시 mutation 없음.

### Invest Screener US Activation (ROB-204)

US `consecutive_gainers` 스크리너는 `invest_screener_snapshots`를 통해 스냅샷 기반 결과를 제공합니다. 첫 번째 US 프로덕션 write는 다음을 요구합니다:

- **additive 컬럼**: `us_symbol_universe.is_common_stock` (nullable Boolean, alembic: `1a2b3c4d5e6f`)
- **분류 CLI**: `scripts/sync_us_common_stock_flags.py` — NASDAQ Trader 파일 기반, dry-run 기본값
- **bounded commit**: `scripts/build_invest_screener_snapshots.py --market us --all --common-stocks-only --commit` (dry-run 증거 + 리뷰어 승인 후에만)
- **user-facing warning**: `app/services/invest_view_model/screener_service.py` — `dataState ∈ {"missing", "stale"}`이면 `"미국 스크리너 데이터 준비중 — 일부 결과만 표시됩니다."` 경고 추가
- **프론트엔드 chip**: `frontend/invest/src/desktop/screener/ScreenerFreshnessLine.tsx` — non-fresh `dataState`에 freshness chip 렌더링
- **Prefect flow**: `app/flows/invest_screener_snapshots_us_flow.py` — `INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED` 환경 변수로 게이트 (기본 `False` → dry-run). **배포 등록은 이 PR에 포함되지 않음.**

**운영 활성화 절차**: `docs/runbooks/invest-screener-snapshots.md` §7 (US 활성화) 및 §8 (Prefect 배포, 연기됨) 참고.

**안전 경계**: TaskIQ 반복 스케줄 없음. 브로커/주문/감시 mutation 없음. DB write는 `InvestScreenerSnapshotsRepository.upsert`만 허용.

**ROB-207 activation:** `POST /trading/api/research-reports/ingest/bulk` is the news-ingestor → auto_trader bridge (token-authed via `RESEARCH_REPORTS_INGEST_TOKEN`). `GET /trading/api/research-reports/freshness` returns the readiness signal. A TaskIQ task `research_reports.ingest_bulk_smoke` is registered but ships **scheduleless**; production recurrence lives in `robin-prefect-automations` and remains `paused=true` until the unpause checklist in `docs/runbooks/research-reports-integration.md` is satisfied. Production cutover (`paused=false`) is approval-gated.

### Hermes Report Generation (ROB-287)

`auto_trader`는 결정적 evidence + persistence 레이어, Hermes는 LLM reasoning + composition. 4개 MCP tool (`investment_report_prepare_bundle` / `..._get_hermes_context` / `investment_stage_artifacts_ingest_from_hermes` / `..._create_from_hermes_composition`) 와 동일 surface를 HTTP transport로도 제공.

- **MCP tools**: `app/mcp_server/tooling/investment_hermes_handlers.py`
- **HTTP routes**: `app/routers/investment_hermes_http.py` — prefix `/trading/api/investment-reports/hermes/`
- **AuthMiddleware token branch**: `app/middleware/auth.py` — `HERMES_INGEST_PATH_PREFIX` 라인. 토큰 미설정 → 403, 잘못된 토큰 → 401.
- **서비스**: `app/services/investment_stages/{hermes_context,hermes_ingest}.py`
- **Prefect flow**: `app/flows/hermes_bundle_preparation_flow.py` — `SnapshotBundleEnsureService.ensure(...)` 호출하여 Hermes가 pull할 신선한 bundle 보장. Default disabled.
- **런북**: `docs/runbooks/hermes-report-generation.md`

**Env / config 게이트 (모두 default off)**:
- `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED` — MCP tools + HTTP endpoints + prep flow 공통 게이트
- `HERMES_INGEST_TOKEN` / `HERMES_INGEST_TOKEN_HEADER` — HTTP transport shared secret (header default `X-Hermes-Ingest-Token`). 운영 secret manager에 배치, repo에 commit 금지.
- `HERMES_BUNDLE_PREPARATION_ENABLED` — Prefect flow operational gate. False면 `{"status": "disabled", ...}` 로 dry-run 종료.

**안전 경계**: 모든 endpoint는 service-layer를 통해서만 쓰기. 어떤 경로도 broker/order/watch/order-intent mutation 도달 안 함. PR #898 static import guard가 `app/services/action_report/snapshot_backed/` + `app/services/investment_stages/` 전체에서 in-process LLM provider 재도입 차단.

**운영 활성화 절차**: `docs/runbooks/hermes-report-generation.md` §3 (non-prod) / §4 (prod cutover). Prefect 배포 등록은 `robin-prefect-automations`, paused-by-default. 실 Hermes JSON-over-wire round-trip 검증 후 ROB-287 Done.

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

### 데이터베이스 정규화 구조

**주식 정보와 분석 결과 분리:**

```
stock_info (마스터 테이블)        stock_analysis_results (분석 결과)
├── id (PK)                      ├── id (PK)
├── symbol (UNIQUE)              ├── stock_info_id (FK) → stock_info.id
├── name                         ├── model_name
├── instrument_type              ├── decision (buy/hold/sell)
├── exchange                     ├── confidence (0-100)
├── sector                       ├── price_analysis (4가지 범위)
├── market_cap                   ├── reasons (JSON)
└── is_active                    ├── detailed_text (markdown)
                                 └── prompt
```

**장점:**
- 종목 정보 중복 방지
- 종목별 분석 히스토리 추적 용이
- `stock_info_service.py`의 `create_stock_if_not_exists`로 자동 생성/조회

**조회 패턴:**
- 최신 분석: Correlated Subquery 또는 Window Function 사용
- 히스토리: `stock_info_id`로 JOIN하여 시간순 정렬

### 해외주식 심볼 변환 시스템

**배경:** 해외주식 심볼은 서비스마다 다른 구분자를 사용함 (예: 버크셔 해서웨이 B)
- Yahoo Finance: `BRK-B` (하이픈)
- 한국투자증권 API: `BRK/B` (슬래시)
- DB 저장 형식: `BRK.B` (점) ← **기준**

**구조:**
```
app/core/symbol.py              # 심볼 변환 유틸리티
├── to_kis_symbol()             # DB → KIS API (. → /)
├── to_yahoo_symbol()           # DB → Yahoo Finance (. → -)
└── to_db_symbol()              # 외부 → DB (- 또는 / → .)
```

**적용된 파일:**
- `app/services/kis.py` - KIS API 호출 시 자동 변환
- `app/services/yahoo.py` - Yahoo Finance 호출 시 자동 변환
- `app/tasks/kis.py` - 심볼 비교 시 정규화
- `app/services/kis_holdings_service.py` - 보유주식 조회 시 정규화
- `app/services/kis_trading_service.py` - 매도 주문 시 정규화

**DB 테이블 (해외주식 심볼 저장):**
| 테이블 | 컬럼 | 설명 |
|--------|------|------|
| `stock_info` | `symbol` | 종목 마스터 |
| `manual_holdings` | `ticker` | 수동 잔고 (토스 등) |
| `stock_aliases` | `ticker` | 종목 별칭 매핑 |
| `symbol_trade_settings` | `symbol` | 종목별 거래 설정 |

**마이그레이션:** 기존 데이터가 `-` 또는 `/` 형식이면 `.` 형식으로 변환 필요
```bash
# scripts/migrate_symbols_to_dot_format.sql 실행
psql -d your_db -f scripts/migrate_symbols_to_dot_format.sql
```

**테스트:**
```bash
uv run pytest tests/test_symbol_conversion.py -v
```

### API 서비스 클라이언트

```
app/services/
├── upbit.py                 # Upbit API (암호화폐)
├── yahoo.py                 # Yahoo Finance API
├── kis.py                   # 한국투자증권 API (30,000+ 라인)
├── upbit_websocket.py       # Upbit 실시간 시세
└── redis_token_manager.py   # Redis 기반 토큰 관리
```

**주의사항:**
- `kis.py`는 매우 큰 파일(30,000+ 라인)이므로 읽을 때 offset/limit 사용
- KIS 분봉 API는 `time_unit` 파라미터가 제대로 작동하지 않는 알려진 이슈 있음
- Upbit은 실시간 WebSocket과 REST API 모두 지원

### 데이터 구조

**KR/US 심볼 유니버스 (DB 단일 소스):**
```
app/services/
├── kr_symbol_universe_service.py   # KR 심볼 조회/동기화
├── upbit_symbol_universe_service.py # Upbit 심볼 조회/동기화
└── us_symbol_universe_service.py   # US 심볼 조회/동기화

scripts/
├── sync_kr_symbol_universe.py      # KR 유니버스 DB 동기화
├── sync_upbit_symbol_universe.py   # Upbit 유니버스 DB 동기화
└── sync_us_symbol_universe.py      # US 유니버스 DB 동기화

DB Tables:
├── kr_symbol_universe
├── upbit_symbol_universe
└── us_symbol_universe
```

**특징:**
- KR/US 종목 검색 및 라우팅은 DB 테이블을 단일 소스로 사용
- Upbit 심볼/마켓 해석도 DB 테이블(`upbit_symbol_universe`)을 단일 소스로 사용
- 배포/마이그레이션 직후 심볼 유니버스 sync 스크립트 실행이 필요

## 브랜치 & PR 워크플로우

### 브랜치 보호
- **main**, **production** 브랜치는 보호됨 — 직접 push 금지
- 모든 코드 변경은 Pull Request를 통해 머지

### 브랜치 역할
- **main**: 개발 브랜치 (모든 PR의 base)
- **production**: 배포 브랜치 (GHCR 이미지 빌드 트리거)

### 브랜치 네이밍
```
feature/<task-id>-<설명>     # 새 기능 (예: feature/ROB-16-branch-protection)
fix/<task-id>-<설명>         # 버그 수정
chore/<설명>                 # 유지보수
```

### 워크플로우
1. `main` 브랜치에서 feature branch 생성
2. 코드 변경 후 커밋 (`Co-Authored-By: Paperclip <noreply@paperclip.ing>`)
3. PR 생성 (base: `main`)
4. 리뷰 후 머지
5. 배포 시 `main` → `production` 머지

### Worktree 운영 규칙 (필수)

**canonical repo `/Users/mgh3326/work/auto_trader` 는 항상 `main` 체크아웃 고정. 배포 머지 시에만 `production` 으로 일시 전환.** canonical repo에서 feature/fix 브랜치를 체크아웃하거나 작업하지 않습니다.

코드 변경은 worktree에서 수행합니다. 다만 **새 Linear 이슈/병렬 작업**과 **같은 Linear 이슈의 follow-up**을 구분합니다:

- 새 Linear 이슈, 병렬 작업, 기존 worktree가 dirty인 경우, 또는 이전 diff/reference를 보존해야 하는 경우: 새 worktree를 만듭니다.
- 같은 Linear 이슈의 follow-up이고 기존 issue worktree가 clean하며 재사용 가능하면: 기존 worktree를 재사용해도 됩니다. 물리 worktree를 매번 새로 만드는 것이 필수는 아닙니다.
- PR이 merge된 브랜치 위에서 계속 커밋하지 않습니다. follow-up 작업은 항상 최신 `origin/main` 기준 새 branch로 시작합니다.
- worktree 재사용 전에는 `git status --short`, 필요한 diff/reference 백업, `git fetch --prune`을 먼저 확인합니다.

```bash
# canonical repo 업데이트
cd /Users/mgh3326/work/auto_trader
git fetch --prune origin
git switch main
git pull --ff-only

# 새 Linear 이슈/병렬 작업: 새 worktree 생성
git worktree add ../auto_trader.<issue-id> -b <branch-name> origin/main

# 같은 Linear 이슈 follow-up: 기존 worktree가 clean하면 재사용
cd /Users/mgh3326/work/auto_trader.<issue-id>
git status --short
git fetch --prune origin
git switch -c <new-followup-branch> origin/main

# PR 머지 후 정리 (필요 diff/reference가 없고 clean한 상태에서)
cd /Users/mgh3326/work/auto_trader
git worktree remove ../auto_trader.<issue-id>
git branch -D <branch-name>
```

- **표준 worktree 경로**: `/Users/mgh3326/work/auto_trader.<issue-id>` (예: `/Users/mgh3326/work/auto_trader.rob-287`)
- 이전 경로 `.claude/worktrees/`, `~/.claude/worktrees/`, `~/auto_trader/.worktrees/` 는 deprecated — 남아 있다면 표준 경로로 이관하거나 prune
- 이관: `git worktree move <old-path> <new-path>` (dirty 없는 상태에서)
- 삭제된 원격 브랜치(`upstream gone`) 는 주기적으로 `git fetch --prune && git branch -vv | grep ': gone\]'` 로 확인하고 정리

## 주요 워크플로우

### 1. 새로운 서비스 분석기 추가

```python
# app/analysis/service_analyzers.py에 추가
class NewServiceAnalyzer(Analyzer):
    """새로운 서비스 분석기"""

    async def _collect_data(self, symbol: str):
        """데이터 수집 로직 구현"""
        # 1. 일봉/현재가/기본정보 수집
        df_historical = await new_service.fetch_ohlcv(symbol)
        df_current = await new_service.fetch_price(symbol)
        fundamental_info = await new_service.fetch_fundamental_info(symbol)

        # 2. 분봉 수집 (있는 경우)
        minute_candles = await new_service.fetch_minute_candles(symbol)

        # 3. 데이터 병합
        df_merged = DataProcessor.merge_historical_and_current(
            df_historical, df_current
        )

        return df_merged, fundamental_info, minute_candles

    async def analyze_symbols(self, symbols: List[str]):
        """심볼 분석"""
        for symbol in symbols:
            df, info, candles = await self._collect_data(symbol)

            # 공통 Analyzer의 analyze_and_save 사용
            result, model = await self.analyze_and_save(
                df=df,
                symbol=symbol,
                name=symbol,
                instrument_type="new_type",
                currency="$",
                unit_shares="주",
                fundamental_info=info,
                minute_candles=candles,
            )
```

### 2. 데이터베이스 모델 변경

```bash
# 1. app/models/에서 모델 수정
# 2. 마이그레이션 자동 생성
uv run alembic revision --autogenerate -m "description"

# 3. 생성된 마이그레이션 파일 검토 (alembic/versions/)
# 4. 적용
uv run alembic upgrade head

# 5. 문제 시 롤백
uv run alembic downgrade -1
```

**중요:** Alembic은 async 엔진 사용 - `alembic/env.py` 참고

### 3. JSON 분석 결과 사용

```python
from app.analysis.service_analyzers import UpbitAnalyzer

analyzer = UpbitAnalyzer()

# JSON 형식으로 분석 (StockAnalysisResult 테이블에 저장)
result, model = await analyzer.analyze_coins_json(["비트코인"])

if hasattr(result, 'decision'):
    # 구조화된 JSON 응답
    print(f"결정: {result.decision}")  # buy/hold/sell
    print(f"신뢰도: {result.confidence}%")  # 0-100
    print(f"근거: {result.reasons}")  # 최대 3개
    print(f"매수 범위: {result.price_analysis.appropriate_buy_range}")
else:
    # fallback 텍스트 응답 (PromptResult 테이블에 저장)
    print(f"텍스트 응답: {result}")
```

### 4. Redis 모델 제한 관리

```bash
# Redis 연결 확인
docker compose exec redis redis-cli ping

# 제한 키 조회
docker compose exec redis redis-cli --scan --pattern "model_rate_limit:*"

# 특정 제한 키 TTL 확인
docker compose exec redis redis-cli ttl "model_rate_limit:<model>:<masked_api_key>"
```

## 환경 변수

**필수 환경 변수 (.env 파일):**

```bash

# 한국투자증권 (KIS)
KIS_APP_KEY=xxx
KIS_APP_SECRET=xxx
KIS_ACCOUNT_NO=12345678-01            # 선택사항

# Upbit
UPBIT_ACCESS_KEY=xxx
UPBIT_SECRET_KEY=xxx
UPBIT_BUY_AMOUNT=100000               # 분할 매수 금액 (기본 10만원)
UPBIT_MIN_KRW_BALANCE=105000          # 최소 KRW 잔고

# 데이터베이스
DATABASE_URL=postgresql+asyncpg://user:pass@localhost/dbname

# Redis (방법 1: URL로 통합 설정 - 권장)
REDIS_URL=redis://localhost:6379/0

# Redis (방법 2: 개별 설정 - REDIS_URL이 없을 때만 사용)
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=                       # 선택사항
REDIS_SSL=false

# Telegram (선택사항)
TELEGRAM_TOKEN=xxx
TELEGRAM_CHAT_IDS_STR=chat_id1,chat_id2

# OpenDART (선택사항)
OPENDART_API_KEY=xxx

# TradingAgents advisory runner (ROB-9, 선택사항)
TRADINGAGENTS_REPO_PATH=/path/to/TradingAgents
TRADINGAGENTS_PYTHON=/path/to/TradingAgents/.venv/bin/python
TRADINGAGENTS_RUNNER_PATH=/path/to/run_auto_trader_research.py
# 전체 설정과 안전 제약은 docs/plans/ROB-9-tradingagents-auto-trader-integration-plan.md 참고
```

## 테스트 작성

**테스트 마커 사용:**
```python
import pytest

@pytest.mark.unit
async def test_analyzer_prompt_generation():
    """단위 테스트"""
    pass

@pytest.mark.integration
async def test_upbit_api_integration():
    """통합 테스트 (실제 API 호출)"""
    pass

@pytest.mark.slow
async def test_heavy_operation():
    """느린 테스트"""
    pass
```

**테스트 실행:**
```bash
pytest tests/test_file.py -v -k "test_name"  # 특정 테스트
pytest tests/ -v -m "not integration"        # 통합 테스트 제외
pytest tests/ -v -m "not slow"               # 느린 테스트 제외
```

## 웹 대시보드

### JSON 분석 대시보드
- URL: `http://localhost:8000/analysis-json/`
- 기능: 필터링, 페이지네이션, 상세 모달
- 통계: 투자 결정 분포, 평균 신뢰도

### 최신 종목 정보 대시보드
- URL: `http://localhost:8000/stock-latest/`
- 기능: 종목별 최신 분석 결과 조회

### Trading Policy YAML 단일 소스 (ROB-646)

`config/trading_policy.yaml` = 매매 판단 임계값 단일 소스 (ROB-643 플레이북 policy_keys에서 시드). **operator PR로만 편집 — 쓰기 도구 없음.**

- **스키마/로더**: `app/schemas/trading_policy.py`, `app/services/trading_policy_service.py`
- **MCP 도구**: `get_trading_policy(market, lane)` — market×lane 임계값 + `{version, content_hash}` echo; 없는 키는 `success=false, error=unknown_key`
- **버전 스탬핑 계약**: 판정 기록(evidence_snapshot·trade_retrospectives·forecast)은 `{version, content_hash}` 인용. `get_operating_briefing`가 run-start에 `policy_version` echo.
- **강제 범위**: 섹터 클러스터 집중도 cap만 매수 프리뷰에서 코드 검사 (`sector_concentration` 필드, **fail-open** — 경고만, 차단 안 함). 나머지 임계값은 advisory.
- **관할**: 판단 임계값 전용. fail-closed 코드 가드(손실매도/ladder/RSI 스코어링)·`symbol_trade_settings`(라이브 사이징)·`trade_profile`(dead)와 분리. migration 0.

## 문제 해결

### KIS 분봉 API 문제
- **증상:** `time_unit` 파라미터가 제대로 작동하지 않아 모든 시간대에서 동일한 데이터 반환
- **해결:** 현재 KIS API 자체의 문제로 향후 업데이트 대기 중
- **대응:** 분봉 수집 실패 시에도 일봉 데이터로 분석 진행

### Redis 연결 실패
- Docker Compose로 Redis 실행: `docker compose up -d redis`
- 연결 테스트: `docker compose exec redis redis-cli ping`
- 로그 확인: `docker compose logs redis`

### 데이터베이스 마이그레이션 충돌
```bash
# 현재 버전 확인
uv run alembic current

# 특정 버전으로 롤백
uv run alembic downgrade <revision>

# 마이그레이션 히스토리 확인
uv run alembic history
```


## 참고 문서

프로젝트 루트의 다음 문서들을 참고하세요:

- `docs/archive/JSON_ANALYSIS_README.md` - JSON 분석 시스템 상세 가이드
- `docs/archive/ANALYSIS_REFACTOR_README.md` - 분석 시스템 아키텍처 및 Redis 모델 제한
- `STOCK_INFO_GUIDE.md` - 데이터베이스 정규화 구조 및 SQL 쿼리 패턴
- `UPBIT_WEBSOCKET_README.md` - Upbit WebSocket 실시간 시세
- `DEPLOYMENT.md` - 배포 가이드
- `DOCKER_USAGE.md` - Docker 사용법

## gstack

Use the `/browse` skill from gstack for all web browsing. Never use `mcp__claude-in-chrome__*` tools.

Available gstack skills: `/office-hours`, `/plan-ceo-review`, `/plan-eng-review`, `/plan-design-review`, `/design-consultation`, `/design-shotgun`, `/design-html`, `/review`, `/ship`, `/land-and-deploy`, `/canary`, `/benchmark`, `/browse`, `/connect-chrome`, `/qa`, `/qa-only`, `/design-review`, `/setup-browser-cookies`, `/setup-deploy`, `/setup-gbrain`, `/retro`, `/investigate`, `/document-release`, `/document-generate`, `/codex`, `/cso`, `/autoplan`, `/plan-devex-review`, `/devex-review`, `/careful`, `/freeze`, `/guard`, `/unfreeze`, `/gstack-upgrade`, `/learn`.
