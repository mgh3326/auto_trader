# 브로커·레저 계약

이 파일은 작업 분야와 무관하게 작업 시작 전에 끝까지 읽어야 하는 필독 계약의 일부다. 찾아보기는 탐색 보조일 뿐 선택 읽기 면제가 아니다.

## 목차

- Alpaca Paper 실행 레저 (ROB-84)
- Binance Demo Order Ledger (ROB-298)
- Binance Demo 라이브 실행 루프 — 전략 플러그형 (ROB-993)
- Execution Ledger HTTP Ingest (fillwire P0)
- KIS WebSocket Mock Smoke (ROB-104)
- kis_mock 귀속 사슬 — pre-submit 강제
- KIS Live Order Fill-Evidence Gate (ROB-395)
- KIS Day-Order Expiry by Accept-Session × Side (ROB-671)
- US & Crypto Live Order Fill-Evidence Gate (ROB-407)
- Kiwoom Mock Account Lifecycle (ROB-97 / ROB-319)
- Kiwoom Live Read-Only Market Data (Stage 1)
- NHPLUG Mock Read-Only Foundation (Stage 1)
- 토스증권 Open API (ROB-529)

## 기준 원문 계약

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

### Execution Ledger HTTP Ingest (fillwire P0)

체결 수집 Go 데몬 `fillwire` 로의 이관을 위한 **P0 계약**. Go 는 DB 에 직접 쓰지 않고
auto_trader 의 토큰 인증 HTTP 표면으로 원장에 넣는다. 이 단계는 계약 + Python 모니터
sink 스위치까지이며 **Go 0줄 · Redis Streams 0줄 · 스케줄러 0건 · 마이그레이션 0건**이다.

- **엔드포인트**: `POST /trading/api/execution-ledger/fills/ingest` (배치 1..200),
  `POST /trading/api/execution-ledger/reconcile/trigger` (dry_run 기본 true)
- **라우터/스키마**: `app/routers/execution_ledger_ingest.py`,
  `app/schemas/execution_ledger_ingest.py`
- **공유 서비스**: `app/services/execution_ledger/fill_ingest.py` (upsert + 다운스트림
  오케스트레이션 — 모니터와 HTTP 라우터가 **같은 함수**를 쓴다),
  `app/services/execution_ledger/fill_sinks.py` (`WS_LEDGER_SINK`),
  `app/services/reconcile_trigger.py` (재연결 트리거 + dedupe)
- **미들웨어**: `app/middleware/auth.py` — `EXECUTION_LEDGER_INGEST_PATH_PREFIX`
  (`EXECUTION_LEDGER_INGEST_TOKEN` 미설정 403 · 오류 401 · 세션 쿠키 대체 불가)
- **런북**: `docs/runbooks/execution-ledger-ingest.md`

**계약/경계**:
- 멱등키는 기존 DB unique key `(broker, account_mode, venue, broker_order_id, fill_seq)`
  **그대로**. `venue`/`account_mode` 를 빼지 말 것. 같은 payload 재전송은 `unchanged` +
  같은 `row_id` 라 **부분 재전송이 안전**하다.
- 항목별 savepoint — 한 fill 의 검증/DB 실패가 배치의 다른 fill 을 롤백하지 않는다.
  응답 항목은 정확히 `{status, row_id, reason}` 이며 요청 순서와 대응한다.
- 🔴 **행의 `source` 는 항상 `websocket` 으로 강제**된다. producer 가 항목에
  `reconciler`/`manual_import` 를 넣어도 무시 — 이 transport 자체가 websocket tap 이다.
  envelope `source`(`fillwire`/`websocket_monitor`)는 transport provenance 이고 행에 쓰이지
  않는다. **outer `source_run_id` 가 transport run authority**이며 item 값을 이긴다.
- `raw_payload_json` 은 optional. 없으면 canonical 필드로 `FillOrder` 를 재구성해
  **알림은 그대로 나간다**. raw frame 은 Upbit 누적 `executed_volume` 같은 추가 문맥용이며
  없다는 이유로 알림을 생략하지 않는다.
- 🔴 **원장 커밋이 권한자.** 다운스트림(알림·rung 투영) 실패는 커밋된 fill 을 rejected 로
  세탁하거나 롤백/재삽입하지 않는다.
- reconcile 트리거는 **기존 커널만** 호출(KR=`kis_live_reconcile_orders_impl`,
  US/crypto=`live_reconcile_orders_impl`). `reason` 은 exact literal `"reconnect"`.
  `backfilled` 는 **실제 커밋된 booking**(`action=booked*`)만 세며 **dry-run 은 항상 0**.
  같은 market 60초 dedupe(프로세스 로컬 monotonic, 동시 요청도 커널 1회 진입).
- **`WS_LEDGER_SINK=db|http`(기본 `db`)**: `http` 는 같은 정규화 upsert 를 localhost
  ingest 로 POST 하고 **다운스트림은 API 서버가 소유**(모니터 중복 실행 없음). 단
  sink 소유권은 `http` **AND** `EXECUTION_LEDGER_COMMIT_ENABLED` 이며, gate off 면
  sink 가 아예 호출되지 않으므로 모니터가 알림을 계속 소유한다(알림 1회, write 0). 실패는
  bounded 재시도 큐 → 소진/큐 상한 시 **직접 DB 로 fail-open** + `sink_fallback` 증가,
  종료 시 flush. 남는 유실 경계는 DB fallback 자체 실패뿐이며 ERROR + `sink_fallback_failed`
  로 드러난다.
- 🔴 **`WS_LEDGER_SINK_URL` 은 토큰을 실어 보낸다**: scheme `http` + loopback host
  (`127.0.0.1`/`localhost`/`::1`) + **정확한** ingest path + userinfo/query/fragment 금지를
  **생성 시와 전송 직전 두 번** 검증하고 `follow_redirects=False` 를 명시 고정한다. 거부된
  URL 은 **소켓을 열기 전에** DB 로 fail-open 하며 로그에 토큰·URL 을 남기지 않는다.

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

### Kiwoom Mock Account Lifecycle (ROB-97 / ROB-319)

Kiwoom **모의투자** 전용 MCP order/account lifecycle. KR 7개 도구는 `account_mode="kiwoom_mock"`(KRX). **US는 ROB-867로 확장** — `kiwoom_mock_us_*` 변형(account_mode="kiwoom_mock_us", US 전용 앱키 4종 env, order-id 9자리 — 07-20 full 스모크 실측 확정).

- **ROB-1340 ACCEPTANCE authority cessation**: confirmed ACCEPTANCE의 BUY→즉시
  journal→resting read→CANCEL→즉시 journal→reconcile은 하나의 PostgreSQL
  `CoordinationScope` 안에서만 실행한다. cancel 직전 ownership 상실 시 cancel을 보내지
  않고 `MANDATORY_CANCEL_BLOCKED_BY_AUTHORITY` live-order-risk를 cycle JSON에 먼저
  append한 뒤 기존 Telegram notifier로 알린다. exact BUY ACK부터 cancel 종료 사이의
  다른 예외도 `POST_ACK_CANCEL_WINDOW_EXCEPTION`으로 같은 write-before-notify 계약을
  따른다. `return_code=0`인데 `ord_no`가 빈 값/비숫자인 접수 응답은 그보다 앞선 전용
  `POST_ACK_ORDER_ID_UNREADABLE` risk로 기록하며 `order_id=None`, local correlation과
  bounded raw-response summary만 남긴 뒤 알린다. 프로세스 제어 `BaseException`은 관측 뒤
  그대로 다시 올린다. authority start/terminal은 전용
  append-only DB table의 current-cycle 완전 열거와 exact receipt coverage가 있어야만
  `RELEASE_VERIFIED`다. 빈 lock/close/PID 부재만으로 승격하거나 receipt를 reporter가
  만들지 않는다. 상세 복구·신뢰 한계는
  `docs/runbooks/kiwoom-b0x-bounded-send.md`를 따른다.

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

- **클라이언트**: `app/services/brokers/toss/` — `transport.py`(host allowlist `openapi.tossinvest.com` + **https 강제**, 3xx 거부), `auth.TossOAuthTokenManager`(OAuth, **client당 유효 토큰 1개**라 Redis 공유+단일비행+failed-token double-check, ROB-262 패턴), `rate_limiter`(`TOSS_RATE_LIMITER_BACKEND=local` 기본의 프로세스 전역 싱글톤; `redis` opt-in은 client-id fingerprint별 원자 슬라이딩 윈도 공유, 장애 시 local로 fail-closed 강등; 그룹별 TPS, 09:00–09:10 ORDER 3TPS), `errors.parse_toss_response`(envelope + non-json typed), `client.TossReadClient`(read + place/modify/cancel)
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


## 유지 규약

새 기능·안전 경계·계약을 추가하거나 변경할 때에는 관련 분야 계약을 같은 PR에서 갱신하고, 필요하면 두 진입점의 최소 하드룰과 명시적 필독 목록도 함께 갱신한다.
