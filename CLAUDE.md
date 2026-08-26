# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

AI 기반 자동 거래 분석 시스템으로, 다양한 금융 데이터를 수집하고 out-of-process MCP consumer(claude 세션 등)를 통해 투자 분석을 제공합니다.

**주요 특징:**
- 다중 시장 지원: 국내주식(KIS), 해외주식(KIS/Yahoo Finance), 암호화폐(Upbit)
- 다중 시간대 분석: 일봉 200개 + 분봉(60분/5분/1분)
- AI 분석: out-of-process MCP consumer(claude 세션 등)가 담당 (런타임은 in-process LLM provider 미탑재 — ROB-501 가드)

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
- **계정 전체 조회 (ROB-993 R3 추가)**: `get_all_positions()`/`get_all_open_orders()` — `symbol` 파라미터 생략 시 Binance가 전종목 데이터 반환하는 것을 그대로 노출(additive, 기존 `get_position(symbol=...)`/`get_open_orders(symbol=...)` 동작 불변). 공유 Demo 계정에서 신호 symbol이 아닌 다른 symbol의 기존 포지션/미체결도 감지해야 하는 소비자(ROB-993 strategy loop)용
- **CLI**: `scripts/binance_futures_demo_smoke.py` (default-disabled, 5 modes)
- **런북**: `docs/runbooks/binance-futures-demo-smoke.md`

### Binance Demo 라이브 실행 루프 — 전략 플러그형 (ROB-993)

실시간 1m→4h bar 집계(H1 오프라인 builder `research/nautilus_scalping/rob974_features.py`의
`build_complete_4h`를 그대로 재사용 — UTC 경계·결측=NO_SIGNAL·forward-fill 금지 동일 시맨틱) +
플러그형 전략 인터페이스(`evaluate(bars_4h_multi_symbol) -> Signal|None`) + kill switch +
`BinanceFuturesDemoExecutionClient`(ROB-298) 배선. 전략 무관 인프라 — S3 신호엔진 어댑터(ROB-980)는
별도 커밋(미포함), 기본 플러그인 `NullStrategy`는 항상 `None`.

- **패키지**: `app/services/brokers/binance/demo_strategy_loop/` — `bars.py`(H1 재사용 + 1m fetch),
  `strategy.py`(`Signal`/`StrategyPlugin`/`NullStrategy`), `kill_switch.py`(최대동시포지션1 +
  연속 SL/UTC일), `sizing.py`, `execution.py`(open MARKET + reduceOnly close round-trip, ROB-298
  smoke CLI와 동일 lifecycle), `correlation.py`, `orchestrator.py`(`run_tick`)
- **CLI**: `scripts/binance_demo_strategy_loop.py` (default-disabled, `--once`/`--loop`/
  `--paper-signal`/`--readiness`) — `--paper-signal`이 ROB-993 e2e 스모크 경로(주문 1건 데모 왕복)
- **env**: `BINANCE_DEMO_STRATEGY_LOOP_ENABLED`(기본 false) — 기존 `BINANCE_FUTURES_DEMO_*`
  자격증명/호스트 allowlist 그대로 상속, 신규 자격증명 표면 없음
- **kill switch**: env 게이트 + 동시 포지션 1 상한(`count_open_lifecycles` 재사용) + 연속 SL 2회/UTC일
  정지(자체 `strategy_loop_tag`로 스코프, closed root의 `extra_metadata.exit_reason` 워크)
- **하드 인바리언트(R2/R3 적대검증 경화)**: leg notional `[6,10]` USDT·동시포지션 1·연속SL 2는 CLI로
  덮어쓸 수 없는 상수(`sizing.LEG_NOTIONAL_CAP_*`/`kill_switch.LOCKED_LIMITS`), `run_tick`이 네트워크/DB
  전에 자체 검증 — **R3**: cap 입력값뿐 아니라 LOT_SIZE floor 이후 **실현 notional**도 재검증(캡 안이어도
  floor로 $6 밑으로 내려갈 수 있음, 키우지 않고 `sizing_blocked`). `execute_signal_round_trip`은
  reservation 직후 broker-flat pre-submit gate(공유 Demo 계정) + 자기 fill delta 귀속 close 수량 +
  submit/poll 응답 전부 symbol/side/qty/reduceOnly echo 검증(`BrokerEchoMismatch`) + open root를
  reconcile 전부 통과 전까지 `filled`(blocking) 유지(조기 `closed` 전이 금지) 적용 — **R3**: flat gate가
  신호 symbol 하나가 아니라 **계정 전체**(`BinanceFuturesDemoExecutionClient.get_all_positions`/
  `get_all_open_orders`, symbol 파라미터 생략 시 전종목 반환 — 신규 추가)를 보고, reservation 직후 +
  order-test 이후 submit 직전 **두 번** 재확인(완전한 TOCTOU 제거는 아님, 런북 §5). multi-symbol
  decision bucket도 전종목 동일 `close_ts` 아니면 전략 미호출. 상세=런북 §8(R2)·§9(R3)
- **학습루프 척추**: `correlation_id`(`binance-demo-strategy-loop:<tag>:<hash>`) → ledger → `forecast_save`
- **런북**: `docs/runbooks/binance-demo-strategy-loop.md` (§5 — 공유 Demo 계정 간섭 주의: 프로덕션
  demo-scalping 봇과 동일 자격증명 공유 시 계정단 상태 충돌 가능)
- **스케줄러 등록 없음** — CLI 수동 가동만, `--loop`도 operator 소유 foreground 프로세스

### KIS WebSocket Mock Smoke (ROB-104)

`scripts/kis_websocket_mock_smoke.py` — KIS 모의 WebSocket 핸드셰이크 검증 (주문/체결/Redis publish 없음).

- **CLI**: `uv run python -m scripts.kis_websocket_mock_smoke`
- **런북**: `docs/runbooks/kis-websocket-mock-smoke.md`
- **이벤트 태깅**: `app/services/kis_websocket_internal/events.py::build_lifecycle_event` (ROB-100 `OrderLifecycleEvent`)

### kis_mock 귀속 사슬 — pre-submit 강제

`kis_mock` 주문은 **브로커 전송 전에** 귀속이 확정돼야 한다. 확정 못 하면 주문이 나가지 않는다(fail-closed).

- **모델**: `app/models/review.KISMockSignalLedger` (`review.kis_mock_signal_ledger`) — `correlation_id`/`strategy`/`signal_source` **NOT NULL + 공백거부 CHECK**
- **서비스**: `app/services/kis_mock_attribution.py` — `resolve_attribution`(순수, 실패 시 `MissingAttribution`) / `record_signal` / `mark_signal_outcome`
- **조회**: `app/services/kis_mock_attribution_chain.py::load_attribution_chain` — gap 코드 `signal_missing`/`order_missing`/`order_unattributed`/`reconcile_missing`
- **게이트 위치**: `order_execution._execute_and_record` 최상단 (브로커 read 보다도 앞)
- **런북**: `docs/runbooks/kis-mock-attribution-chain.md`

**호출자 계약(파괴적)**: `place_order(is_mock=True)` 는 이제 `strategy` 필수 — 없으면 `error_code="attribution_required"`. `mirror_cohort="mock_counterfactual"` 은 레인 라벨 자동 판정. `thesis` 는 여전히 불필요.

**주의**: `kis_mock_order_ledger.correlation_id`/`strategy` 는 과거 NULL 행 때문에 nullable 유지 — DB 제약은 pre-submit 신호 테이블에 걸려 있다. 원장 백필은 별건.

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

### KIS Day-Order Expiry by Accept-Session × Side (ROB-671)

`kis_live_place_order` 응답의 `expected_expiry`/`expiry_reason` 및
`kis_live_get_order_history` 행의 `expiry_reason` 은 **접수 세션 × 매매구분**으로
결정된다. 순수 offline 분류기(`app/services/brokers/kis/live_order_expiry.py` —
stdlib only, 브로커/DB/네트워크/캘린더 import 없음, 주문 hot path 무네트워크 보장):

- 세션 창(KST, 마감 배타): premarket 08:00–08:50 / regular 09:00–15:30 /
  nxt_after 16:00–20:00 / 그 외 off.
- **정규장 SELL 은 NXT 로 연장**되어 20:00 KST 까지 유효(SOR 현금매도 NXT carry).
  → "내 매도주문이 죽었나?" 오판 금지. reason=`nxt_carry`.
- 정규장 BUY 는 **보수적 기본값 20:00 KST** (오늘 동작 유지), reason=
  `regular_buy_conservative_20_00`. ROB-657 이 관측한 정규장 매수 15:30 사멸은
  세션 만료가 아니라 **D+2 미결제(현금) 취소**(ROB-625 KRW variant)일 수 있어
  **원인 미확정**. 공격적 `15:30` 다운그레이드(reason=`regular_buy_unsettled_15_30`)
  는 구현되어 있으나 `KIS_REGULAR_BUY_UNSETTLED_EXPIRY_1530=true` (기본 off)
  게이트 뒤에 있으며, **라이브 측정으로 원인 확정 후에만** 활성화한다.
- premarket/nxt_after → 20:00(`nxt_carry`). off 창 접수 → 20:00(`unknown_session`).
- US(해외) 주문 history 행의 `expiry_reason` 은 `us_day_order` placeholder(NXT 없음).

reconcile 종료 분류(`classify_day_order_expiry`)는 변경 없음 — 여전히
evidence-first / fail-closed.


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

Kiwoom **모의투자** 전용 MCP order/account lifecycle. KR 7개 도구는 `account_mode="kiwoom_mock"`(KRX). **US는 ROB-867로 확장** — `kiwoom_mock_us_*` 변형(account_mode="kiwoom_mock_us", US 전용 앱키 4종 env, order-id 9자리 — 07-20 full 스모크 실측 확정).

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
- **KR 도구는 KRX only**: `NXT`/`SOR`/비-KRX 거부 (네트워크 호출 전). US 도구는 별도 `kiwoom_mock_us_*` 경로만 사용
- **No secrets printed**: CLI는 missing env key **이름만** 보고, 값 출력 없음
- **Cancel-before-submit**: `full` 모드는 cancel이 wired이기에만 실주문 제출; finally-block에서 항상 cancel 시도 후 reconcile

### Kiwoom Live Read-Only Market Data (Stage 1)

🔴 **레포에서 주문 가능한 live 호스트 `https://api.kiwoom.com` 에 붙는 유일한 클라이언트.** 차트만 읽으며 그 외에는 아무것도 못 한다.

- **클라이언트**: `app/services/brokers/kiwoom/live_market_data.KiwoomLiveReadOnlyClient` (+ 전용 `KiwoomLiveReadOnlyAuthClient`). 🔴 `KiwoomMockClient`/`auth.KiwoomAuthClient` 를 **확장·수정하지 않으며 import 하지도 않는다** — mock 단언은 그대로다
- **비교 하니스**: `app/services/brokers/kiwoom/chart_compare.py` — mock/live 필드 대조 + KIS 프로즌 샘플 3자 판정
- **CLI**: `scripts/kiwoom_live_readonly_compare.py` (default-disabled, `--confirm-live-read` 필수)
- **런북**: `docs/runbooks/kiwoom-live-readonly-marketdata.md`

**안전 경계 (4층)**:
- **Default-disabled**: `KIWOOM_LIVE_MARKETDATA_ENABLED=false` 기본. 🔴 게이트는 **생성자가 아니라 dispatch 시점** 검사 — `from_app_settings` 우회 직접 생성도 전송 불가
- **allowlist**: api-id `ka10080/81/82/83` (차트 4종) + path `/api/dostk/chart` 만. 🔴 토큰 해석·소켓 오픈 **이전** 검사. 주문 TR(kt10000~kt10003)·계좌 TR 전부 거부
- **호스트/경로 고정 + 전송 직전 재검증**: build 후 `send` 직전 `request.url.host`·`request.url.path` **둘 다** 재확인. 🔴 `follow_redirects=False` 를 OAuth·chart 양쪽에 **명시 고정**(httpx 기본값 의존 금지 — 3xx 는 검증 통과 요청이 다른 호스트로 갈 유일한 경로)
- **계좌번호 부재 (3중)**: ① Settings live 표면은 `app_key`/`app_secret`/`base_url` **3개뿐**, `kiwoom_account_no` 없음 ② AST 가드가 신규 live 모듈에서 주문 상수·주문 모듈 import·`kiwoom_account_no`/`KIWOOM_ACCOUNT_NO` 참조를 **문자열 우회 포함** 금지 ③ 전용 env 파일에 `KIWOOM_ACCOUNT_NO` 를 넣지 않아 **프로세스 환경에 값이 아예 없음**
- 🔴 **보장 강도 = "우발 방지 + 정적 검출"**. **"구조적 불가능"이 아니다** — 계좌번호는 배포 env 파일에 여전히 존재하고 Settings 한 줄이면 도달 가능해진다. AST 가드는 **그 한 줄을 빌드 실패로 만드는 장치**다
- **자격증명**: 전용 최소 파일 `.env.kiwoom-readonly.native`(4키, ACCOUNT_NO·DATABASE_URL 없음). 🔴 `ENV_FILE=.env.prod` 금지(CLI가 파일명 `prod` 거부)
- **Redis 격리**: OAuth 토큰 캐시는 `--redis-url` 로 일회용 인스턴스 지정 — 배포 공유 캐시에 쓰지 않는다
- **Rate limit 실측(2026-08-03, mock)**: 2.0s/1.0s/0.5s OK · 0.2s/0.05s `HTTPStatusError` → 임계는 0.5~0.2초 사이, 운영 기본 **2.0초**
- **스케줄러 등록 없음** — CLI 수동 실행만. 🔴 **Stage 2(대량 수집·DB 저장)는 별도 승인**

**동일성 실측(2026-08-03, 20종목 × 일봉 600 + 5분봉 900)**: 행 커버리지 40/40 동일, 비교 셀 252,000 중 불일치 36(99.9857%) — 🔴 **전부 형성 중인 최신 봉**(장중 2초 시차)이며 **최신 봉 제외 시 100.000000%**. 상폐 `051170` 은 live 에서 1행 반환. KIS 3자 대조에서 068270 은 2026-06-03 경계로 223건 어긋나지만 **live·mock 결과가 동일**하며 원인은 수정주가 역산 **반올림 규칙 차이**(약 0.004%) — 어느 쪽이 옳은지는 `UNDETERMINED`. 함의: **Kiwoom↔KIS 과거 수정주가 완전일치 대조는 실패하므로 허용오차 필요**

### NHPLUG Mock Read-Only Foundation (Stage 1)

`app/services/brokers/nhplug/` and `scripts/nhplug_mock_smoke.py` expose a bounded read-only foundation only: account discovery (`/n2/acctinfo`), KR balance, and KR current quote. There are **no** order methods, MCP order tools, ledgers, reconcile paths, or scheduler registrations.

- **Data host × account-type double discriminator**: data requests use only `https://moapi.nhplug.com:8443`; the scheme, host, and port are checked again on the built request immediately before `send`. `/n2/acctinfo` establishes an allowlist containing only `acct_type="03"`; `01`/`02` are denied, and a number with conflicting returned types rejects the account response. `NHPLUG_MOCK_ACCOUNT_NO` is untrusted until it appears in that broker response, and account-scoped reads recheck it again immediately before send.
- **Exceptional OAuth physical separation**: token issue/revoke must reach `https://api.nhplug.com:8443`, but only `nhplug/auth.py` may name that host and it allowlists exactly `POST /oauth2/token` and `POST /oauth2/revoke`. The data client does not import it and has no production-host constant. Both clients apply the master gate at dispatch and explicitly use `follow_redirects=False`; this also protects APP KEY/SECRET custom headers from cross-origin redirect forwarding.
- **Default-disabled**: `NHPLUG_MOCK_ENABLED=true` is required at every OAuth and data dispatch; unset is fail-closed. The smoke CLI requires an operator-created `.env.nhplug-mock.native`-style file with exactly `NHPLUG_APP_KEY`, `NHPLUG_APP_SECRET`, and `NHPLUG_MOCK_ACCOUNT_NO`; it rejects `prod` file names/`ENV_FILE` and any extra key (including `DATABASE_URL`). It prints key names and safe response shape only, never values.
- **No vendor fail-open configuration**: do not import the vendor `nhplug` SDK and never read `NHPLUG_BASE_URL` / `NHPLUG_AUTH_URL`. Host constants are local and static guards reject SDK imports, production-host literals outside auth, override-env strings (including constant concatenation), and known order endpoints/TRs.
- **Guarantee strength**: this is **"accidental prevention + static detection," not structural impossibility**. The same APP KEY can access operating accounts, OAuth tokens are issued on the operating host, and `/n2/acctinfo` necessarily returns operating accounts alongside mock accounts. The `03` allowlist is our check, not a vendor-enforced isolation boundary. See `docs/runbooks/nhplug-mock-smoke.md`.

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

### Screener pick log (prospective bakeoff)

`discover_buy_candidates_fanout` itself still performs no writes. When
`SCREENER_PICK_LOG_ENABLED=true` (default false), an outer observer in
`buy_candidate_fanout_registration` records returned per-source picks to
`review.screener_pick_log`. Fail-open. Prices are exact decimal text, never
float. Additive migration only — operator runs `alembic upgrade head`.
No scheduler. Not a policy/weight input.

### screen_stocks_snapshot / screen_stocks_enrich MCP 도구 분리 (ROB-1309)

Sentry 실측(p50 38.11s / p95 54.73s, 호출당 ~214 HTTP call, 120s 예산 내 타임아웃 8건)에서
`screen_stocks_snapshot`이 기본 경로에서 매 호출마다 섹터 lazy-fill + 애널리스트 컨센서스 +
실시간가 fetch를 무조건 수행하던 것이 원인으로 확인되어, 두 도구로 분리했습니다.

| | `screen_stocks_snapshot` (기존 이름, 계약 변경) | `screen_stocks_enrich` (신규) |
|---|---|---|
| 기본 동작 | **DB-only** — `invest_screener_snapshots`/`invest_crypto_screener_snapshots` 읽기 + 필터/페이지네이션만 | 동일 preset/filter/pagination 파이프라인 실행 후 라이브 enrichment |
| HTTP 호출 | **0회** (KR/US/crypto 전부) — 섹터 lazy-fill 없음, 애널리스트 컨센서스 fetch 없음, quoteSummary/timeseries/crumb 없음, 실시간가 fetch 없음 | 심볼당 섹터(KR Naver/US yfinance) + 애널리스트 컨센서스(KR/US 둘 다 Redis 캐시-어사이드, 아래 참고) + 실시간가 fetch |
| `min_analyst_count`/`min_analyst_buy_count` | **거부** — `{"error": ..., "redirectTool": "screen_stocks_enrich"}` fail-closed (무시하거나 네트워크 호출하지 않음) | 지원 — 페이지네이션 전에 컨센서스 COUNT를 해석해 필터링 |
| 응답의 `analysisContext`/`analystLabel` | 없음 | 있음 (`enrich_snapshot_page` 결과) |
| write 부작용 | 없음 — `screen_stocks_snapshot`은 KIS-live holdings lookup을 포함해 어떤 external HTTP call도 만들지 않는다(ROB-1309 회귀수정: `isHeld`는 항상 `false`, `exclude_held`는 `screen_stocks_enrich`로 fail-closed redirect) | 섹터 lazy-fill이 기존 `symbol_sectors_service`(ROB-512) 경로로 씀. KIS-live 보유종목 조회 1회(`isHeld`/`exclude_held` 표시용, ROB-1309 이전부터 존재하던 bounded call)도 이제 이 도구에서만 수행됨. `invest_screener_snapshots` 자체에 대한 쓰기는 없음(그 테이블의 유일한 writer는 여전히 `InvestScreenerSnapshotsRepository.upsert`이며, 오프라인 스냅샷 빌더/flow 전용 — 이 두 MCP 도구 어느 쪽도 호출하지 않음) |
| 음성 캐시 | 해당 없음 | 있음 — `app/services/invest_view_model/enrichment_negative_cache.py` (재시도 차단 TTL 30분 + 실패 이력/카운트 보존 TTL 24시간 분리, 에러 분류, 연속 실패 카운트). 실패/스킵은 `meta.enrichment_excluded`에 항상 명시적으로 보고(조용히 사라지지 않음). **연속 3회 이상 실패한 chronic 후보는 그 호출의 `results`에서는 제외되고 `meta.chronic_failure_candidates`에 별도로 non-silent 보고된다** — "절대 제거 안 됨"이 아니라 "제거는 항상 명시적으로 보고되고, 두 TTL 중 어느 쪽이든 경과하거나 성공 시 자동 회복되어 영구 은닉이 불가능"이 정확한 계약이다. **유니버스 mutation은 의도적으로 구현하지 않음** — 아래 "negative cache = universe cleanup 범위" 참고 |
| 공유 로직 | `app/mcp_server/tooling/screener_snapshot_tool.py::_build_snapshot_page` → `build_screener_results(..., snapshot_only=True)` (DB-only 빌드/필터/페이지네이션; 스냅샷이 없거나/stale이거나 스냅샷 분기 자체가 없는 preset/market 조합은 generic `ScreenerService.list_screening`으로 fall-through하지 않고 빈 결과 + 명시적 warning으로 fail-closed) | 동일 `_build_snapshot_page`를 `snapshot_only=False`(기본값)로 재사용 — snapshot이 없는 일부 preset(예: `consecutive_gainers`/`growth_expectation`(US)/`crypto_high_volume` 파티션 미적재)에 한해 live discovery로 fall-through하는 pre-existing 동작을 유지한다. live fallback은 오직 이 explicit 도구를 통해서만 노출된다 |

**구현**: `app/mcp_server/tooling/screener_snapshot_tool.py`(DB-only 도구 + 공유 빌더),
`app/mcp_server/tooling/screener_enrich_tool.py`(신규 enrichment 도구),
`app/services/invest_view_model/enrichment_negative_cache.py`(신규 음성 캐시).

**US 애널리스트 컨센서스 캐시(ROB-1309 완결)**: `analyst_consensus_cache.py`는 이제 KR(Naver, KST-date
bucket)뿐 아니라 US(yfinance, `analyze_cache.PROVIDER_YFINANCE` US/Eastern-date bucket)도 동일한
Redis cache-aside로 캐싱한다 — `_PROVIDER_BY_MARKET = {"kr": PROVIDER_NAVER, "us": PROVIDER_YFINANCE}`.
캐시 hit 시 US도 KR과 동일하게 전체 yfinance opinion fetch(`analyst_price_targets` +
`recommendations` + `upgrades_downgrades` + `info`)를 건너뛰고, upside 재계산용 실시간가만
`app.services.brokers.yahoo.client.fetch_fast_info`(가벼운 `fast_info` 단일 호출)로 갱신한다.

**negative cache = "universe cleanup" 범위 (의도적 설계 결정)**: 원 요구사항은 "실패/상장폐지 심볼에
대한 negative cache + universe cleanup"이었다. 이 레포는 `kr_symbol_universe`/`us_symbol_universe`
행을 실제로 mutate하는 "universe cleanup"을 구현하지 **않았다** — 대신 요구사항 자체가 명시한
제약("must not permanently hide valid symbols")과 이 레포의 기존 관례(ROB-1236 `halted_suspect`가
정지 의심 종목을 DB mutation 없이 **탐지+보고**만 하는 것과 동일 패턴)를 따라, "universe cleanup"을
**TTL 경과 시 자동 회복되는 활성-fetch 대상에서의 일시 제외**로 해석했다: `enrichment_negative_cache`는
재시도 차단 TTL(`NEGATIVE_CACHE_TTL_SECONDS`, 30분)과 실패 이력/카운트 보존 TTL
(`NEGATIVE_CACHE_HISTORY_TTL_SECONDS`, 24시간)을 분리해 둔다(bounded) — 실패를 분류하고
(error_class), `meta.enrichment_excluded`로 항상 보고하며(non-silent), 성공 시
`record_success`가 즉시 엔트리를 지워 자동 해제된다. **연속 3회 이상 실패(`_CHRONIC_FAILURE_THRESHOLD`)한
chronic 후보는 그 호출의 `screen_stocks_enrich` `results`에서는 제외되지만**(DB/영구 mutation은
아님 — 다음 호출에서 재시도 TTL이 지나 있거나 성공하면 즉시 복귀), **그 제외는 항상
`meta.chronic_failure_candidates`(및 `meta.enrichment_excluded`)로 non-silent 보고된다** —
"조용히 사라짐"이 아니라 "제거는 항상 보고되고, 두 TTL 중 어느 쪽이든 지나거나 성공하면 자동
회복되어 영구 은닉이 불가능"이 정확한 계약이다. `meta.chronic_failure_candidates`는 운영자가
직접 `kr_symbol_universe`/`us_symbol_universe`를 검토할 수 있는 advisory 신호이기도 하며, 이
신호 자체가 자동 삭제/비활성화를 트리거하지는 않는다. 실제 DB 테이블 mutation(예: soft-delete
플래그, 별도 정리 스크립트)이 필요하다고 판단되면 별도 Linear 이슈로 분리해야 한다 — 이 PR
범위에서는 스키마 변경/마이그레이션을 추가하지 않았다.

**주의**: `halted_suspect`(ROB-1236) 시맨틱은 이 분리와 무관합니다 — `screen_stocks_snapshot`/
`screen_stocks_enrich` 어느 쪽도 `halt_filter.py`/`analysis_analyze.py`/`buy_candidate_fanout.py`를
import하거나 건드리지 않습니다(halted_suspect 판정은 `screen_stocks`/`analyze_stock` 전용 경로).

**ROB-207 activation:** `POST /trading/api/research-reports/ingest/bulk` is the news-ingestor → auto_trader bridge (token-authed via `RESEARCH_REPORTS_INGEST_TOKEN`). `GET /trading/api/research-reports/freshness` returns the readiness signal. A TaskIQ task `research_reports.ingest_bulk_smoke` is registered but ships **scheduleless**; production recurrence lives in `robin-prefect-automations` and remains `paused=true` until the unpause checklist in `docs/runbooks/research-reports-integration.md` is satisfied. Production cutover (`paused=false`) is approval-gated.

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

### Negative-class(기각 코호트) 기록 — `decision_bucket` (ROB-1283)

"매수 후보가 정말 없었나"에 정본 데이터로 답하기 위한 기각 기록 경로. 🔴 **관측 전용**
(주문·승인 경로 영향 0).

- **어휘 정본**: `app/models/decision_vocabulary.DECISION_BUCKETS` — 리포트 아이템 CHECK ·
  Pydantic 스키마 · `review.trade_forecasts` CHECK 가 **같은 튜플**에서 생성됨(3층 드리프트 불가)
- **기록**: `forecast_save(decision_bucket="deferred_no_action", ...)` — 세션이 실제로 부르는
  표면. bucket + resolvable target + review_date + outcome/brier 가 한 행이라 리포트 아이템이
  없어도 **채점 가능**(고아 아님)
- **조회**: `get_forecasts(decision_bucket=...)`; `summary.by_decision_bucket` 의
  `unclassified` 는 **사각지대 크기**이지 0 이 아님
- **가드**: `get_operating_briefing` → `negative_class_recording` 섹션. route-stamp
  (`operator-compliance/v1`)가 브리핑 응답을 통째로 박제하므로 **운영자 레포 변경 0으로**
  세션 준수 스탬프에 도달
- **진단**: `scripts/diagnose_negative_class_recording.py` (SELECT only, exit 1 = stalled)
- **런북**: `docs/runbooks/negative-class-recording.md`

**주의**:
- 🔴 2026-06-15~ 결손 구간은 **메우지 않는다**. `gap.backfilled=false` 로 명시 보고 —
  가짜 연속성 금지. 이 구간을 가로지르는 코호트는 정확히 그만큼 불완전
- 🔴 원인은 핸들러 파손이 아니라 **호출 지점 부재**(라이브 프롬프트 5종이 `deferred_no_action`
  어휘만 쓰고 그걸 기록하는 도구 이름을 대지 않음). 기록 재개는 **운영자 레포 프롬프트 수정**이
  선행돼야 함 — 이 레포 변경만으로는 완결되지 않는다
- 🔴 bucket 없는 일반 forecast 는 카운트 안 됨. 실제 정지가 숨은 방식이 정확히 이것
  (`trade_forecasts` 는 66일 내내 바빴다)
- `decision_bucket=NULL` = "미분류"이지 "기각 아님"이 아니다. 오타는 fail-closed(거부)

### 정지 종목 오염 차단 — `halted_suspect` (ROB-1236)

일봉이 **3거래일 연속 죽어 있으면**(`volume=0` **또는** `high==low==close` 이면서
직전 close 와 동일) `data_state: "halted_suspect"`. 🔴 **`fresh` 금지 · 지표 null ·
스크리너/정책표에서 제외.** 실사례 = 000880 한화(8거래일 동결인데 `fresh` + RSI 35.40 로
매수 후보 rank 2 랭킹).

- **판정기**: `app/services/halt_detection.py` (순수·stdlib, DB/네트워크/시계 없음)
- **소비자 3곳**: `analyze_stock_impl::_apply_halt_suspect`(indicators·support_resistance
  = None, quote/top-level 양쪽 data_state 덮어씀) · `screening/halt_filter.py`
  (`screen_stocks_unified` 단일 깔때기) · `scripts/policy_table/adapters/{kr,us,crypto}.py`
- **런북**: `docs/runbooks/halted-suspect-data-state.md`

**주의**:
- 🔴 **`halted_suspect` 는 확정이 아니라 의심.** KRX 거래정지 마스터가 레포에 **없다**
  (`krx_halt_master: "unavailable"` 를 응답에 명시). 확정 정지로 단정 금지.
- 🔴 **N=3 은 양방향 오차를 감수한 값** — 1~2일 정지는 못 잡고(위음성), 3일 연속 무거래
  초저유동 종목은 잡힌다(위양성). 그래서 **제외는 항상 심볼·근거와 함께 보고**된다
  (`meta.halted_suspect_excluded` / `universe.halted_suspect`). 조용한 삭제 금지.
- **상한가/하한가 잠김**은 `open=high=low=close` 지만 직전 close 와 가격이 다르므로
  잡히지 않는다 — 0-변동 조건의 "직전 close 동일" 절을 제거하지 말 것.
- 봉 이력 조회 실패는 **fail-open**(행 유지 + warning). DB 장애는 정지의 증거가 아니다.
- 🔴 스크리너는 **최신봉 거래량 > 0 이면 이력 조회를 건너뛴다**(장중 일봉 캐시 우회 →
  100행 스크린이 KIS 라이브 100회를 때리는 것 방지). 감수하는 구멍 = 거래량 있는
  0-변동 구간. analyze/정책표는 이력을 무조건 읽으므로 그쪽에서 잡힌다.

### analyze quick fast projection (ROB-1311)

`analyze_stock_batch(quick=True)` 는 full analyzer의 formatter-only 변형이 아니다.
MCP registration → `analysis_tool_handlers.analyze_stock_batch_impl` →
`analysis_quick.load_quick_projection_batch` 로 분기해 `daily_candles` DB에서 모든
요청 심볼의 일봉 이력을 batch read하고, 가격/OHLCV/RSI/지지·저항을 로컬 계산한다.
기존 compact consumer가 요구하는 `decision_history`와 `earnings` 의미도 각각
set-based review/market-events read model로 batch read해 canonical 심볼에 붙인다.
KR/US는 시장별 window query 1회씩, crypto는 instrument identity 1회와 candle query
1회를 사용하며, history/earnings read model을 포함한 batch당 DB execution 상한은
12회로 고정한다. 외부 HTTP는 0회다.

Quick allowlist는 `symbol`, `market_type`, `source`, `current_price`, 최신 `ohlcv`,
`rsi_14`, `supports`, `resistances`, `decision_history`, `earnings`,
ROB-1048 freshness envelope 및 `halt_suspect`다. 뉴스/profile/consensus/
recommendation/holdings와 provider 기반 earnings는 quick에서 실행하지 않는다.
`quick=False` 는 기존 `analysis_screening._analyze_stock_impl`
→ `analysis_analyze.analyze_stock_impl` 호출과 full output을 유지한다.

🔴 **`current_price`는 라이브 시세가 아니다.** quick의 `current_price`는
`daily_candles`의 가장 최근 **마감된** 일봉 종가이며 `data_state="stale"`,
`data_state_reason="db_only_projection"`로 항상 스탬프된다. 라이브 가격·세션
상태(NXT 체결가능 등)가 필요하면 반드시 `get_quote`를 호출할 것 — quick만으로
판단하지 말 것.

**PR #1915 축소 필드 (quick 전용, `quick=False`는 불변)**: 구 quick 요약이
가지고 있던 `nxt_tradable`/`nxt_tradable_source`/`nxt_tradable_asof`/
`nxt_tradable_stale`(ROB-668) · `price_source`/`session`/`session_state`/
`krx_prev_close`/`change_pct`(ROB-725/ROB-888) · `venue`/`quote_asof`/`delayed`
(quote provenance) · `price_data_state`(ROB-1048) · `fresh_artifact_exists`
(ROB-648) · `consensus`/`recommendation`/`position`/뉴스/profile/provider
earnings는 전부 quick allowlist에서 제거됐다. 라이브 가격/세션 provenance가
필요하면 `get_quote`를 사용할 것 — quick은 그 값들을 더 이상 담지 않는다.

🔴 ROB-1236: quick도 모든 심볼의 일봉 이력을 반드시 읽고 `classify_ohlcv_frame`을
적용한다. 동결 3세션이면 `data_state=halted_suspect`, RSI와 supports/resistances는
null이며 `halt_suspect` 근거를 보존한다. DB 장애는 해당 행을 `missing`으로 남기며
외부 provider로 우회하지 않는다.

### Telegram 승인 콜백 durable inbox (W5)

`POST /trading/api/telegram/callback` 은 지금까지 승인 워크플로 전체(재검증 →
브로커 제출 → Telegram 메시지 수정)를 **요청 스레드 안에서 인라인 실행**했다.
Sentry 프로덕션 7일 실측: n=44, avg 3.365s / p50 2.738s / p95 12.707s / max
13.593s, child 집계는 `http.client`(359 spans, 86.90s)가 DB(3,106 spans, 4.47s)를
압도. 더 큰 문제는 지연이 아니라 **내구성**이다 — taskiq-redis `ListQueueBroker`
는 LPUSH/BRPOP 이라 워커가 죽기 전에 메시지가 Redis 에서 이미 사라진다.

**PostgreSQL 이 권한자, TaskIQ 는 opaque job UUID 를 나르는 best-effort 깨우기.**
Redis 를 잃으면 지연을 잃지 클릭을 잃지 않는다.

- **테이블**: `review.telegram_callback_inbox` and
  `review.telegram_callback_recovery_cursor` (`app/models/telegram_callback_inbox.py`)
- **마이그레이션**: `alembic/versions/20260821_w5_telegram_callback_inbox.py` (additive)
- **패키지**: `app/services/order_proposals/callback_inbox/` — `contracts`(닫힌 어휘·digest·lock key) ·
  `repository`(service 전용) · `service`(유일한 writer) · `locks`(job advisory lock) ·
  `ingress` · `worker` · `recovery` · `observability`(텔레메트리 allowlist) ·
  `result_boundary.py` · `taskiq_receiver_boundary.py`
- **TaskIQ**: `app/tasks/telegram_callback_inbox_tasks.py` — `order_proposals.telegram_callback_job`
  (per-job) + `order_proposals.telegram_callback_recovery` (**scheduleless 출고**)
- **런북**: `docs/runbooks/telegram-callback-durable-inbox.md`

Accepted canonical inputs retain the post-normalization execution core and the
pre-existing downstream authorization gates. Normalization now also adds the
R37 exact numeric identifier trust boundary before durable authority or core
execution. Published-binding preflight, single-use nonce, commit lease,
target-mutation lock, approval hash, and fresh preview remain downstream gates.

**게이트 3개 전부 default false**:
- `ORDER_PROPOSALS_TELEGRAM_CALLBACK_DURABLE_ENABLED` — durable ingress
- `ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED` — per-job 워커
- `ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_SCHEDULE_ENABLED` — 복구 스윕 cron **및** 실행

🔴 durable ingress 는 worker/recovery 게이트가 **둘 다** 켜져 있지 않으면 sanitized
503 으로 트래픽을 거부한다(설정 레벨 가드 — **프로세스 생존 확인은 런북 §4 절차**).
활성화 순서: 마이그레이션 → 코드 배포 → worker 게이트 → recovery 게이트 + **스케줄러
재시작**(schedule 라벨은 import 시점 고정) → ingress 게이트.

**재시도 대수 (🔴 이걸 바꾸기 전에 런북 §5 를 읽을 것)**:
콜백 코어는 모든 예외를 `{"handled": False, "reason": "internal_error"}` 로 삼킨다.
그 문자열은 **브로커 leg 가 시작되지 않았다는 증거가 아니다** —
`revalidate_and_submit` 가 브로커에 닿은 뒤 commit 전에 던져진 예외도 같은 결과이고,
롤백은 nonce 를 **미소비** 상태로, published binding 을 **유효** 상태로 남긴다.
재시도하면 합법으로 보이면서 두 번 제출된다.

따라서 재실행 가능한 유일한 부류는 **코어에 진입하지 않았음이 증명된 실패**뿐:
- `retry_wait` ← **worker-owned pre-core phase 실패만**. 현재 명시적으로
  `PreCoreFailure` 를 만드는 경로는 코어 진입 전 notifier 해석 실패뿐이고,
  `schedule_retry` 가 조건부 UPDATE 로 DB 에서 `state='processing'` +
  `handler_entered_at`/`handler_completed_at`/`terminal_state_pending` 전부 NULL
  임을 재확인해야 기록된다. 🔴 **핸들러가 반환하는
  `mutation_not_started`/`retry`/`retryable`/`safe_to_retry` 는 진단용이며 재실행
  권한을 전혀 만들지 못한다**(`IGNORED_HANDLER_RETRY_KEYS`) — 이미 mutate 한
  핸들러도 똑같이 반환할 수 있기 때문이다. 코어 진입 마커 이후의 모든
  예외·결과·reason 은 terminal(succeeded/discarded/dead_letter)일 뿐 재시도 없음.
  저장 envelope 복원이 불가능하면 `discarded/envelope_invalid`, 현재 chat
  allowlist 에서 빠졌으면 `discarded/chat_revoked` 이며 둘 다 재시도하지 않는다
- `succeeded` ← `handled=True` (`results: ["unverified"]` 포함 — 모호한 *전송*은
  proposal/order 상태머신 소관이고, 콜백 재실행은 해결이 아니라 중복 위험)
- `discarded` ← 명시적 비즈니스 거부(nonce_replay/expired/guard_blocked/…), chat 취소,
  복원 불가 envelope
- `dead_letter` ← 코어 진입 후 `internal_error`·크래시·contract 위반, 또는 3회 소진.
  **자동 replay 없음. 권한 필드 스크럽됨. 운영자가 새 승인 카드를 발급해야 한다.**

내구 마커 3개가 프로세스가 죽은 뒤에도 이 판정을 가능하게 한다:
`handler_entered_at`(코어 호출 직전 단독 커밋 — "진입 전 사망"과 "진입 후 사망"의
유일한 내구 차이) / `handler_completed_at` + `terminal_state_pending`(코어 반환 직후
단독 커밋 — 마지막 커밋을 잃으면 recovery 가 **재실행이 아니라 스크럽 보수**를 한다).
🔴 세 마커는 **인과 순서**이고(완료⇒진입, 판정⇒진입+완료) DB CHECK
(`ck_..._handler_marker_order`)가 강제하며, **단조(monotonic)** 다 — 어떤 API 도
NULL 로 되돌리지 못한다. 재시도가 합법인 이유는 마커를 지웠기 때문이 아니라 CAS
술어가 애초에 NULL 이었음을 증명했기 때문이다. 또한 `processing` 행은
`started_at` 이 NOT NULL 이어야 한다(`ck_..._processing_started_at`) — NULL 이면
staleness 비교가 영원히 거짓이라 복구 스캔에서 보이지 않는다.

**데이터 최소화 = 스키마 속성**: raw Telegram `Update` 저장 안 함(JSON/JSONB/ARRAY
컬럼 자체가 없음). terminal 도달 즉시 권한/PII 11개 컬럼 NULL — DB CHECK 2개가 강제
(`terminal_scrubbed` / `active_reconstructable`, 둘 다 `CASE WHEN … THEN … ELSE true END`
+ `IS NULL`/`IS NOT NULL` 이라 SQL `UNKNOWN` 이 통과 못 함). 살아남는 건 `update_digest`
(도메인 분리 일방향 dedupe tombstone) · closed-category `outcome`(unknown raw reason은
고정 `unclassified`) · 닫힌 `error_class` 뿐.
W5 애플리케이션 payload에는 canonical job UUID만 들어간다. producer wire
shape는 테스트로 기계 검증되며, 결과·로그·Sentry에는 endpoint별 닫힌
안전 projection만 남는다.

**R37 identifier boundary**: `callback_query.from` 자체가 exact built-in
`dict`여야 하며, `from.id`는 필수 exact built-in `int`이고
`1..2**52-1`만 허용한다. `update_id`는 `None` 또는 exact built-in `int`이며
`1..2_147_483_647`만 허용한다. callback id가 primary여도 present update id는
항상 검증한다. bool·subclass·string·coercible 값은 durable authority 전에
거부한다. DB user id는 canonical decimal `Text`이고 worker는 exact `int`로만
복원하며 active row는 이를 필수로 한다. `callback_query_id`가 있으면
delivery identity primary이고 valid `update_id`는 callback id가 없을 때만
fallback이다. `update_identity_digest`는 별도 active 검증 필드라 같은
callback id의 변경된 update id는 second job이 아니라 conflict이다. terminal
scrub은 `update_identity_digest`를 제거하고 `update_digest` one-way dedupe
tombstone은 남긴다.

**TaskIQ receiver/result boundary**: job wire는 canonical lowercase hyphenated
UUID string 하나의 positional arg와 빈 kwargs, recovery wire는 완전한 empty
envelope다. exact 두 W5 task name은 formatter-load 단계에서 첫 decoded-message
debug/Sentry surface 전에 sanitize되고 마지막 middleware가 다시 sanitize한다.
incoming labels/label-type metadata는 버려지며 SmartRetry는 이 callback들의
retry authority가 아니다. malformed job은 fixed `invalid_job_id`, malformed
recovery는 fixed `error`이고 result/status/worker/recovery projection은 닫힌
어휘다. Untrusted inbound args/kwargs/labels are collapsed on formatter load
before the first decoded-message log/Sentry surface; only canonical producer
envelopes are intentionally emitted. Worker/recovery extras and exception
strings never enter the W5 result backend or W5 logs/Sentry. task body는 exact CancelledError/KeyboardInterrupt/SystemExit만
private category-only signal로 축약해 SmartRetry에 exception을 주지 않는다. final W5
post_execute는 Receiver의 task-exception catch 이후, result save 이전에 fresh safe
exact control을 raise하며 result backend save 및 ack-capable broker의 WHEN_SAVED
ACK 단계에 도달하지 않고 Receiver error log도 없다. `CancelledError`는 해당
callback만 끝내며, TaskIQ result save/ACK와 독립적인 durable
판정은 3개 DB marker와 recovery가 맡는다. shared `auto-trader` worker process에는
신호를 보내지 않는다. 나머지 failure는 fixed safe result로 collapse된다.

Recovery UUID materialization은 exact stdlib `uuid.UUID` 또는 exact
`asyncpg.pgproto.UUID`만 허용한다. owning base descriptor를 통해 fresh stdlib
UUID로 복사하고 subclass/spoof/malformed 값은 render 없이 거부한다. asyncpg는
stdlib fast path 뒤에 lazy import하며 malformed candidate는 bounded
scanned/claimed error slot 하나를 소비한 뒤 sweep을 계속한다.

🔴 **advisory lock 은 브로커 fencing 이 아니고**, PostgreSQL 재시작을 가로지르는 분산
락도 아니다. pending/processing 행은 그 수명 동안 최소 PII 를 보유한다. 실제 프로세스
활성화는 post-deploy 리스크다. 한계는 런북 §7.

### 매수 게이트 A/B shadow (ROB-1301)

KR/US 매수 스크리닝의 **variant B(moderate+ 지지)** 는 계좌 불사용 shadow
실험이다. Variant A(strong 지지 필수)가 라이브 게이트이며 문언·집행은 불변.

- **패키지**: `app/services/buy_gate_ab_shadow/` — 사전등록 스펙·A/B 대칭
  평가·`shadow_buy` forecast 태깅·4주 채점 공식. DB/브로커/제안/워치 import 없음.
- **MCP**: `evaluate_buy_gate_ab_shadow` — observation-only. B-only 후보는
  `forecast_save` kwargs만 반환하고 도구 자체는 쓰지 않는다.
- **금지**: shadow → 제안·주문·워치 승격 0. 채점 전 중간값으로 정책 변경 0.
  승격·자동화 트리거 0. 스케줄러 등록 0.
- **런북**: `docs/runbooks/buy-gate-ab-shadow.md`
- **플레이북**: `docs/playbooks/trading-decision-playbook.md` §3.2 (lane
  sequence 아님)

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
- `app/services/brokers/kis/` - KIS API 호출 시 자동 변환
- `app/services/brokers/yahoo/client.py` - Yahoo Finance 호출 시 자동 변환
- `app/jobs/` - 심볼 비교 시 정규화 (주요 브로커/job 호출부에 배선)
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
app/services/brokers/
├── upbit/       # Upbit API (암호화폐) — client.py, orders.py, public_trades.py
├── yahoo/       # Yahoo Finance API — client.py
├── kis/         # 한국투자증권 API — client.py, account.py, domestic/overseas_orders.py, market_data 등 (파일 분할)
└── toss/ · kiwoom/ · alpaca/ · binance/   # 기타 브로커
app/services/
├── upbit_websocket.py       # Upbit 실시간 시세
└── redis_token_manager.py   # Redis 기반 토큰 관리
```

**주의사항:**
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
2. 코드 변경 후 커밋
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

### CI required check — `ci-required` shadow 집계 (ROB-1294)

branch protection 은 지금도 `lint` · `taskiq-smoke` · `test (3.13, 1..4)` **여섯 이름에 직접**
결합돼 있다. shard 수·lane topology 를 바꾸려면 branch protection 편집이 함께 필요하고,
그 편집을 빠뜨리면 required check 가 영구 pending 이거나 조용히 미강제가 된다.

- **분류기**: `scripts/ci/classify_changes.py` — 변경 경로 → lane 결정적 매핑 (stdlib only)
- **집계기**: `scripts/ci/aggregate_required.py` — 고정 이름 게이트의 판정 로직
- **워크플로우 job**: `.github/workflows/test.yml` 의 `change-classifier` · `ci-required`
- **계약 테스트**: `tests/ci/`
- **런북**: `docs/runbooks/ci-required-aggregator.md`

**현재 상태 = shadow.** 🔴 `ci-required` 는 **required check 가 아니며** 이 작업은 branch
protection/GitHub 설정을 하나도 쓰지 않았다. 분류기 출력은 어떤 job 도 skip 시키지 않는다
(`tests/ci/test_ci_required_workflow_contract.py` 가 이를 기계 검증한다 — 기존 여섯 job 의
표시 이름·matrix·`if` 부재가 변하면 red).

**fail-closed 규칙**: 분류기는 unknown path·rename/copy/delete·공유 CI/config/test 인프라·
빈 change set·base SHA 부재를 전부 `run_all=true` 로 떨어뜨리고, 지정된 SHA 해석 실패·git
실패·malformed diff 는 성공으로 세탁하지 않고 **job 을 red** 로 만든다. 집계기는 child 의
`failure`/`cancelled`/미인가 `skipped`/결과 부재/미지의 결과 문자열을 전부 red 로 보며,
`--authorize-skip` 로 명시된 skip 만 green 이다 (워크플로우는 현재 아무것도 인가하지 않음).

**cutover 는 운영자 전용이며 이번 범위 밖** — 절차는 런북 §5.

## 주요 워크플로우

### 1. 데이터베이스 모델 변경

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

### Trading Policy YAML 단일 소스 (ROB-646)

`config/trading_policy.yaml` = 매매 판단 임계값/decision rule 단일 소스 (ROB-643 플레이북 policy_keys에서 시드). **operator PR로만 편집 — 쓰기 도구 없음.**

- **스키마/로더**: `app/schemas/trading_policy.py`, `app/services/trading_policy_service.py`
- **MCP 도구**: `get_trading_policy(market, lane)` — market×lane 임계값 + lane-scoped `decision_rules` + `{version, content_hash}` echo; 없는 키는 `success=false, error=unknown_key`
- **버전 스탬핑 계약**: 판정 기록(evidence_snapshot·trade_retrospectives·forecast)은 `{version, content_hash}` 인용. `get_operating_briefing`가 run-start에 `policy_version` echo.
- **강제 범위**: 섹터 클러스터 집중도는 매수 프리뷰와 reserve-net consumer에서 계산한다. `order_validation`은 `sector_concentration` 필드로, reserve-net은 `plan.sector_cluster_cap_advisories` 및 생성 proposal의 `source_asof.sector_cluster_cap_advisories`로 **fail-open 경고·기록만** 남긴다. `portfolio.sector_cluster_cap_pct` 초과 자체는 차단 근거가 아니다. 단, 섹터 미상·음수 집중도 데이터·`max_symbols_per_sector_cluster` 등 별도 코드 가드는 그대로 fail-closed다. 나머지 임계값은 advisory.
- **관할**: 판단 임계값/decision rule 전용. fail-closed 코드 가드(손실매도/ladder/RSI 스코어링)·`symbol_trade_settings`(라이브 사이징)·`trade_profile`(dead)와 분리. migration 0.

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

- `docs/archive/JSON_ANALYSIS_README.md` — (아카이브·과거) 삭제된 Gemini analyzer 시절 JSON 분석 문서, 현행 아님
- `docs/archive/ANALYSIS_REFACTOR_README.md` — (아카이브·과거) 삭제된 analyzer/Redis 모델제한 시절 문서, 현행 아님
- `STOCK_INFO_GUIDE.md` - 데이터베이스 정규화 구조 및 SQL 쿼리 패턴
- `UPBIT_WEBSOCKET_README.md` - Upbit WebSocket 실시간 시세
- `DEPLOYMENT.md` - 배포 가이드
- `DOCKER_USAGE.md` - Docker 사용법
