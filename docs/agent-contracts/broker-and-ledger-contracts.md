# 브로커·레저 계약

이 파일은 작업 분야와 무관하게 작업 시작 전에 끝까지 읽어야 하는 필독 계약의 일부다. 찾아보기는 탐색 보조일 뿐 선택 읽기 면제가 아니다.

## 목차

- Alpaca Paper 실행 레저 (ROB-84)
- Binance Demo Order Ledger (ROB-298)
- Binance Demo 라이브 실행 루프 — 전략 플러그형 (ROB-993)
- H5-LS-ENV-v1 Futures Demo 수동 어댑터 (847)
- Execution Ledger HTTP Ingest (fillwire P0)
- Execution Ledger Quarantine (#1175)
- 보호 수량 P 규칙 추종 (#943)
- KIS live KR ledger lots — get_holdings opt-in (#963)
- KIS WebSocket Mock Smoke (ROB-104)
- kis_mock 귀속 사슬 — pre-submit 강제
- kis_mock 4행 Q-46 expired[inference] (#1250)
- KIS Live Order Fill-Evidence Gate (ROB-395)
- KIS Day-Order Expiry by Accept-Session × Side (ROB-671)
- 7-D stale blocker night sweep + KIS expired[inference] (#1112)
- US & Crypto Live Order Fill-Evidence Gate (ROB-407)
- Kiwoom Mock Account Lifecycle (ROB-97 / ROB-319)
- Kiwoom Live Read-Only Market Data (Stage 1)
- NHPLUG Mock Dispatch State Machine (Stage 2)
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
- **D2 remediation root reconcile (#1268)**: `scripts/binance_spot_demo_d2_root_reconcile.py`
  (`app/services/brokers/binance/spot_demo/d2_root_reconcile.py`) — `filled` 인
  `d2_remediation_single` SPOT 루트만 서비스의 `record_closed` → `record_reconciled` 로
  종결한다. preview 기본·`--commit` 적용·`--ids` 정확한 목록(최대 3)·`--reason`/`--actor` 필수.
  모든 id 가 D2 바운드 주문과 일치하고 Spot Demo 읽기 전용 `GET /api/v3/order` 가
  `FILLED`+심볼·사이드·타입·수량·지정가·TIF·주문번호 일치를 보여야 하며, 하나라도 부적격·증거
  불일치·조회 실패면 배치 전체 거부·무변경. 이미 이 도구로 종결된 배치는 no-op. 감사 기록은
  `extra_metadata["d2_root_reconcile"]`(마이그레이션 0). 🔴 직접 SQL·삭제·주문 경로 없음,
  H5 truth gate 범위는 변경하지 않는다. 실행은 운영자 전용(런북 §8)

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

### H5-LS-ENV-v1 Futures Demo 수동 어댑터 (847)

H5는 ROB-993의 StrategyPlugin 인터페이스, complete-only 1m→4h collector,
기존 signed Futures Demo transport와 Demo ledger 서비스를 재사용하는 별도 어댑터다.
ROB-993의 leg notional [6,10], cap 1, kill switch와 ROB-298의 BTC 제외는
그 경로에 그대로 남는다. H5의 독립 상수와 상태 서비스는 H5 identity에서만 쓰인다.

- **표면**: app/services/brokers/binance/h5, scripts/binance_h5_demo.py,
  scripts/binance_h5_weekly_score.py. 수동 CLI만 있으며 scheduler 등록은 없다.
- **배포 이미지 (task 1254)**: H5 모듈이 research.nautilus_scalping.rob974_features를
  import하므로 `Dockerfile.api` 최종 단계는 `research/nautilus_scalping/`만 복사한다
  (`research/`의 나머지는 이미지에 넣지 않는다). 이 COPY는 order permission이 아니다.
  `tests/scripts/test_dockerfile_api_ships_nautilus_scalping.py`가 최종 단계의 COPY
  집합과 `.dockerignore`를 시뮬레이션해 `scripts/binance_h5_*.py`의 import 폐쇄(정적 +
  이미지 파일만 둔 새 인터프리터 import)와 `research/` 범위를 고정한다.
- **호스트/게이트**: exact https://demo-fapi.binance.com 및 H5DemoClient identity를
  HTTP/DB 전에 검사한다. BINANCE_H5_DEMO_ENABLED와
  BINANCE_FUTURES_DEMO_ENABLED는 기본 false이고, 각 runner tick과 모든 주문은
  명시 confirm=True가 필요하다. live 자격증명과 endpoint는 사용하지 않는다.
- **노출/전송**: review.binance_h5_signals와 review.binance_h5_intents는 H5 전용
  서비스가 advisory transaction lock으로 예약한다. 최대 전역 2개, 심볼별 1개,
  외부 포지션과 미체결 주문 시 entry 차단. client order ID를 commit한 뒤
  sending fence를 commit하고 단 한 번 보낸다. 응답 불명은 uncertain으로 보존하고
  broker order ID/client ID와 전계정 position 증거로만 해소한다. 예약된 pre-send
  intent는 재시작 시 자동 전송하지 않는다.
- **수량/청산**: 최신 NAV × 0.01 / 0.05의 notional을 executable quote와
  MARKET_LOT_SIZE로 내림하고 MIN_NOTIONAL 미달 시 차단한다. isolated 1x와 one-way
  모드는 broker readback으로 요구하며 설정 mutation은 없다. 청산은 hard stop,
  completed-bar close stop, time exit, TP 순서이고 모든 청산은 reduceOnly이며
  broker 증명 잔량까지만 보낸다. hard stop은 4h 마감 사이에도 1m 극값으로 검사한다.
- **레저/학습**: 모든 binance_demo_order_ledger 쓰기는
  BinanceDemoLedgerService만 사용한다. H5 correlation과 signal_key는 DFC identity와
  독립이며 forecast는 실제 entry fill 후 저장하고 실제 held close 후 해소한다.
  predeclared 4h opportunity grid의 random control은 오프라인 계산만 수행한다.
- **권한**: 이 코드의 존재는 order permission이 아니다. 별도 operator contract의
  strategy_order_exception 승인과 운영자 확인 전에는 runner를 시작할 수 없다.
  마이그레이션도 별도 운영 절차로 적용한다. 상세는
  docs/runbooks/binance-h5-demo.md를 따른다.
- **NCP 수동 운영·알림 (task 1251)**: 정확한 복붙 절차는
  `docs/runbooks/binance-h5-ncp-manual-playbook.md`(명령의 플래그는
  `tests/scripts/test_binance_h5_ncp_playbook.py`가 각 스크립트의 실제 argparse로
  검증). 데모 플래그 3종(`BINANCE_H5_DEMO_ENABLED`·`BINANCE_FUTURES_DEMO_ENABLED`·
  `BINANCE_H5_ALERT_ENABLED`)은 해당 일회성 컨테이너의 `docker run -e` 에만 둔다 —
  공유 env 파일(`.env.api`) 편집·`--restart`·`-d`·cron/systemd 등록 금지.
  시작 전 `scripts/binance_h5_truth_gate.py --confirm-demo`(서명 GET + SELECT 만,
  주문·쓰기 도달 불가를 AST 테스트로 고정)가 PASS 해야 한다.
- **비USDT 잔고 (#1272, hk 1271 = B)**: Futures Demo 기본 지급분(USDC·BTC)은 같은 서명 GET
  `/fapi/v2/account` 응답의 `multiAssetsMargin` 이 정확히 JSON `false` 일 때만 허용된다(`read_account`).
  true·누락·null·문자열·숫자·읽기 실패는 FAIL. 비USDT 잔고는 양의 유한값·이름 `[A-Z0-9]{1,20}` 이어야 하고,
  있으면 NAV(`totalMarginBalance`)가 USDT `marginBalance` 와 같아야 한다. 트루스 게이트
  `account_isolated_1x` 는 모드를 다시 확인하고 통과 detail 에 `margin_mode=single_asset non_usdt_assets=…`
  를 붙인다(비USDT 잔고 없으면 detail 불변). 읽기 경로·다른 체크·종료 코드 불변. 기록:
  `docs/contracts/h5-deviation-20261008-single-asset-margin-foreign-balances.md`.
- **장애 알림 (default off, `BINANCE_H5_ALERT_ENABLED` 정확히 `true` 일 때만)**:
  `h5/alerting.py`. `stopped`(SIGINT 외의 종료)·`error`(`blocked`/`entry_uncertain`/
  `close_uncertain` 틱)는 러너 안에서, `heartbeat_missed`는 별도 읽기 전용
  `scripts/binance_h5_heartbeat_watch.py`가 `review.binance_h5_lane_state.updated_at`
  (`record_nav` 가 틱마다 갱신)이 N분(기본 10) 이상 묵었을 때 보낸다 — SIGKILL/OOM 은
  이 경로만 잡는다. 채널은 기존 ops 채널 `settings.discord_webhook_alerts`(Hermes 아님,
  신규 provider 없음). 에피소드당 1회·리마인더 최대 6시간 1회·전송 실패 재시도 5분
  간격이며 전송 실패·지연은 러너 동작을 바꾸지 못한다(타임아웃 5초, 예외 삼킴, 틱
  알림은 백그라운드 태스크라 다음 틱을 늦추지 않고 종료 시 drain). 에피소드 키는
  (종류, 버킷)이며 틱 오류의 버킷은 틱 event 라서 예외 클래스가 바뀌어도 같은
  에피소드다. 감시자의 heartbeat 읽기는 15초 데드라인이며 초과는 `unreadable` 알림이다.
  H5 에는 거래소 측 손절이 없으므로 모든 종류가 "포지션이 있으면 손절 감시 중단"을 뜻한다.
  코인 세션이 읽을 H5 상태 도구는 아직 없다 — live-crypto 프로필은 폐쇄 세계이고
  core 15/extension 10 상한이 차 있어 별도 결정이 필요하다.

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

### Execution Ledger Quarantine (#1175)

websocket 탭이 KIS H0STCNI0 **접수** 통지(`CNTG_YN=1`)를 체결로 기록한 phantom 행을
삭제·수동 UPDATE 없이 **격리**한다. fillwire 디코더 수정은 별건(#1172).

- **CLI**: `scripts/quarantine_execution_ledger_rows.py` — preview 기본, `--commit` 적용,
  `--ids` 정확한 10진 id 만(범위·패턴·부호·공백·중복 거부), `--reason`/`--actor` 필수,
  DB 는 `--database-url-env NAME`(권장) 또는 `--database-url` 로 명시(값 출력 금지)
- **서비스/쓰기**: `app/services/execution_ledger/quarantine.py`(순수 판정 + 트랜잭션) →
  `ExecutionLedgerRepository.rows_by_ids`/`mark_quarantined`/`append_quarantine_events`
  (레포지토리가 유일한 쓰기 표면)
- **적격(전부 통과해야 배치 전체 진행)**: 존재 · 미격리 · `source=websocket` · `broker=kis` ·
  `account_mode=live` · `equity_kr` · 저장된 `raw_payload_json` 이 `tr=H0STCNI0` 프레임이고
  `fields[13]`(CNTG_YN) 이 정확히 `1` · 프레임 주문번호/종목이 행과 일치. 하나라도 부적격이면
  **배치 전체 거부·무변경**, 전부 이미 격리면 no-op. `CNTG_YN=2`(실체결)는 항상 거부
- **스키마**: `execution_ledger.quarantined_at/quarantine_reason/quarantined_by`(nullable) +
  CHECK 2종(all-or-nothing · `source='websocket' AND broker='kis'` 만) + append-only
  `review.execution_ledger_quarantine_events`(ledger_id UNIQUE, FK 없음). 마이그레이션
  `20261001_t1175_ledger_quar`, downgrade 는 격리를 잃는다
- 🔴 **격리 행은 terminal**: 트리거가 격리된 행의 모든 UPDATE·DELETE 와(격리 행이 있는 동안)
  TRUNCATE 를 `restrict_violation` 으로 거부한다. 같은 키로 들어오는 변경된 쓰기(바뀐 phantom
  재생, 우연히 키가 같은 실체결·reconciler 행)는 덮어쓰거나 숨기지 않고 **거부되어 운영자 검토로
  드러난다**(HTTP ingest 항목 `rejected` + 경고 로그, reconcile run 실패·`error_summary`).
  동일 재생은 `unchanged`. ingest 경로(repository upsert·commit_fill·router)는 이 기능으로
  바뀌지 않았다. tombstone/재격리 로직은 r4(hk 1224=A)에서 제거
- 🔴 **리더 계약**: fill/lot/evidence/리포트로 원장을 읽는 모든 함수는
  `execution_ledger_in_effect()`(`quarantined_at IS NULL`)를 AND 한다. 업서트 식별 읽기
  (`get_by_key`)·id 워터마크·격리 도구 자신만 예외이며,
  `tests/services/execution_ledger/test_quarantine_mutants.py` 가 디스크에서 리더를 세어
  필터 또는 예외 선언이 없는 새 리더를 red 로 만든다
- **시드 주의**: 격리 전 phantom 이 있는 상태로 깎인 opening seed 는 격리로 고쳐지지 않는다
  (seed 스크립트 preview 재실행 필요)
- **런북**: `docs/runbooks/execution-ledger-quarantine.md`. 실DB 실행은 운영자 전용

### 보호 수량 P 규칙 추종 (#943)

운영자 결정(hk doc 8274 §7): P 는 계산값이며 첫 선언 뒤에는 규칙으로만 바뀌고 알림만 간다. 임시 규칙 = P 는 보유 전량.

- **서비스**: `app/services/protected_position_auto_follow.py` — 모든 P 변경은 `ProtectedQuantityService.save`(origin `operator_cli`, actor 고정 owner `MCP_USER_ID`, 락 안 fresh broker 재조회) 경유
- **킬스위치**: `PROTECTED_POSITION_AUTO_FOLLOW_ENABLED` 기본 false — off 면 head·broker 조회·쓰기·알림 0
- **훅 위치**: 원장 커밋 **이후**만 — `ExecutionLedgerReconciler` 커밋(task·script `--commit`), Toss reconcile 부킹 세션 종료 후. dry-run 에서는 호출 안 함. `source=reconciler`·`account_mode=live` 행만(websocket 은 provisional 이라 무시)
- **레버**: `scripts/protected_positions.py auto-reconcile`(기본 preview, `--commit` 필요) + TaskIQ `protected_positions.auto_follow_reconcile` — 🔴 **코드에 스케줄 없음**. desk 가 NCP systemd timer 로 KR/US 정규장 30분 간격 one-shot CLI 실행(운영자 Q-75, #944 option A). 🔴 timer 는 `--database-url-env NAME` 필수 — `--database-url` 은 수동용(argv 에 비밀 노출). URL 값은 출력·로그 금지, 오류는 변수 이름만
- 🔴 **금지**: 미선언 키 자동 선언, P=0(해제) 재상향, 보유 초과 P, 읽기 경로·send-time guard 에서의 쓰기(`test_auto_follow_writer_is_unreachable_from_read_and_guard_paths` import allowlist 가 강제)
- 🔴 **unobserved (#1061)**: 브로커가 응답했지만 그 키의 보유/매도가능 수량이 판독 불가(Toss `sellable_quantity=None` 등)면 그 키만 `unobserved`(reason `FIELD_unavailable:SYMBOL`) — P·revision 불변, 알림 없음, 레버 exit 1. **held 0 으로 취급 금지**(P 하향·해제 = 위험 방향). 락 안 재조회도 동일(save 롤백). 같은 응답의 다른 Toss 키·미선언 종목은 영향 없음. KIS 리더는 불변(시장 단위 실패)
- **알려진 공백**: Toss 앱 수동 매도는 원장에 안 들어온다 — 레버 실행 전까지 P 가 보유보다 높게 남는다
- **런북**: `docs/runbooks/longterm-lot-protection.md` §Rule-executed P follow · §Desk write CLI

### KIS live KR ledger lots — get_holdings opt-in (#963)

`get_holdings(include_ledger_lots=True)` (default `False`, default output byte-identical — golden test) attaches a read-only `ledger_lots`
block to KIS live KR positions so a live session can use KIS lots and KIS own-open-buy / own-open-sell evidence **without** a KIS broker order read.
The #678 harness denial of `kis_live_get_order_history` is unchanged and is asserted by test; no live.yaml, lane allowlist or robin
allowlist change exists because `get_holdings` is already live-kr core.

- **서비스**: `app/services/execution_ledger/kis_lots.py` — 순수 투영(`build_symbol_block`) + DB 로더(`load_kis_live_kr_lot_blocks`). MCP 레이어는
  `app/mcp_server/tooling/portfolio_ledger_lots.py` 얇은 attach 뿐이다. 브로커 client import·쓰기 없음(AST/소스 가드 테스트).
- **lots**: authoritative 행(`reconciler`/`manual_import`)만의 FIFO 잔여 lot, `cost_method="fifo_remaining_lots_from_ledger"` — **브로커 이동평균 평균단가가 아니다.**
  `websocket` 행은 provisional 이라 lot 에 절대 세지 않는다. authoritative 행이 같은 주문을 덮지 않는 websocket 행만 `provisional_rows_excluded` 에 나열되고, 덮인 중복 행은
  나열 없이 `diagnostics.superseded_websocket_duplicates` 로만 센다.
- **freshness / unknown**: 마지막 성공 non-dry-run KIS reconcile `finished_at` 이 90분 이내여야 `fresh`. `ledger_state` 는 fresh AND authoritative 행 존재 AND 역매도 없음
  AND 원장 순수량 == 같은 응답의 브로커 수량일 때만 `known`. 그 외는 `unknown` + `unknown_reasons`, `lots=null` — **빈 리스트로 표현하지 않는다.**
  브로커 수량 교차검증은 **모든 심볼에** 적용된다 — 같은 fill 이 reconciler 행과 websocket 행(다른 `fill_seq`, #935 Part B)으로 함께 있으면 websocket 행은 주문 단위로 supersede 되어
  이중 계상되지 않고, websocket 행으로**만** 존재하는 fill 은 lot 수를 줄이는 대신 `quantity_mismatch_with_reference`(+ 진단용 `provisional_rows_pending_reconcile`) 로 `unknown` 이 된다.
  websocket 행은 어떤 lot/net 합에도 들어가지 않는다(합산은 진단 `provisional_net_quantity` 뿐). 재현 fixture: `test_kis_lots.py`·`test_kis_lots_db.py` 의 `test_935_*`.
- **open_buy_evidence (S2/S3)**: 당일(KST) `review.kis_live_order_ledger` 비터미널 buy 행(S2), 당일 buy 체결 중 주문 완료가 원장으로 증명되지 않은 것(S3)이 있거나 증거가
  unknown(stale·읽기 실패)이면 `blocking=true`. 전일 이전 비터미널 행은 `presumed_dead_prior_day_buys` 로 보고만 한다(KRX/NXT day order 는 거래일을 넘기지 못한다는 가정).
- **same_day_sell_evidence**: 당일(KST) 매도 체결(authoritative + authoritative 가 덮지 않는 provisional websocket 행, `provisional` 플래그)이 있거나 reconcile 이 stale/없으면 `blocking=true`.
  strategy-lab 판정(hk #963 comment 796/798): 보이는 반대방향 신호가 하나라도 있으면 그날 미배치, 없으면 caveat `kis_same_day_sell_chain_unverified` 부착. 오늘 이후 날짜로 찍힌 행(writer 시계 skew)은
  fail-closed 로 '오늘'로 취급한다(`open_buy_evidence`·`same_day_sell_evidence` 공통).
- **매도측 증거 (task #1087, #1044 운영자 Q-107=A)** — 같은 블록에 DB-only 로 추가되며 KIS 호출·circuit breaker 접촉·신규 도구·LIVE_ALLOWED_TOOLS 변경이 없다.
  - **open_sell_evidence**: `open_buy_evidence` 의 매도 쌍둥이. 당일(KST) `review.kis_live_order_ledger` 비터미널 **sell** 행이면 `own_nonterminal_sell_order_today`,
    당일 매도 체결(authoritative + authoritative 가 덮지 않는 provisional websocket 행) 중 주문 완료가 원장으로 증명되지 않은 것이면 `same_day_sell_fill_order_not_proven_complete`,
    reconcile 없음·90분 초과 stale·주문원장 읽기 실패면 `open_sell_evidence_unknown` — 모두 `blocking=true`. `state` 는 `known|unknown`, `scope="orders_known_to_auto_trader_only"`,
    `external_orders_verifiable=false`. 전일 이전 비터미널 매도는 `presumed_dead_prior_day_sells` 로 보고만 한다. `own_open_sell_order_quantity` 는 당일 비터미널 자기 매도의
    **주문 수량**(미체결 잔량이 아님 — 보수적) 합이며 unknown 이면 `null`.
  - **same_day_buy_evidence**: `same_day_sell_evidence` 의 매수 쌍둥이. 당일(KST) 매수 체결(opening seed=`manual_import` 이면서 `SEED-*` 주문번호인 행만 제외 — 그 밖의 `manual_import` 는 실제 체결로 센다, provisional 포함·`provisional` 플래그)이 있으면
    `same_day_buy_fill_in_ledger`, reconcile stale/없음이면 `same_day_buy_evidence_unknown` — 매도의 same-day chain / wash 판정용 반대방향 시야.
  - **sellable_by_ledger** (+ `sellable_by_ledger_basis`): `ledger_state=known`(fresh·브로커 수량과 순수량 일치) **AND** `open_sell_evidence.state=known` **AND** 모든 당일 자기
    비터미널 매도에 수량이 있을 때만 `원장 순수량 − own_open_sell_order_quantity` 를 `[0, 브로커 수량]` 으로 clamp 한 값, 그 외는 `null` + `unknown_reasons`
    (`ledger_state_unknown`/`open_sell_evidence_unknown`/`own_open_sell_quantity_unknown`). provisional websocket 행은 절대 이 수량에 들어가지 않는다.
    🔴 이 값은 **상한이지 허가가 아니다** — 게이트는 `open_sell_evidence`·`same_day_buy_evidence` 둘 다 non-blocking.
  - 당일 증거 4종(`open_buy_evidence` S3·`same_day_sell_evidence`·`open_sell_evidence`·`same_day_buy_evidence`) 모두 `SEED-*` 가 아닌 `manual_import` 행을
    실제 체결로 센다(#1087 에서 #963 두 뷰도 함께 교정 — blocking 을 더할 뿐 줄이지 않는다).
  - 후속 운영자 프롬프트 PR 이 쓸 caveat 이름: `kis_external_open_orders_unverified`(auto_trader 밖 미체결 매도 불가시), `kis_same_day_buy_chain_unverified`
    (auto_trader 밖 당일 매수는 체결 전까지 불가시). 이 PR 은 프롬프트·`live/CLAUDE.md` 를 바꾸지 않는다.
- 🔴 **`external_orders_verifiable` 는 항상 `false`**: KIS 앱/HTS 등 auto_trader 밖에서 낸 **미체결** 주문은 어떤 DB 읽기로도 보이지 않는다. 이 블록의 통과는
  "부재 증명"이 아니라 "auto_trader 가 아는 범위에 미체결 없음"이다. 운영자 승인 Q-81(#964)=A: 그 잔여는 caveat `kis_external_open_orders_unverified` 로 기록한다.
- **실패 격리**: 블록의 어떤 실패도 `get_holdings` 를 실패시키지 않는다 — 포지션마다 `ledger_state="unknown"`, `unknown_reasons=["ledger_read_failed"]`.
- **테스트**: `tests/services/execution_ledger/test_kis_lots.py`(순수), `test_kis_lots_db.py`(테스트 DB), `tests/mcp_server/test_get_holdings_ledger_lots.py`(golden·opt-in·격리),
  `test_get_holdings_ledger_lots_permissions.py`(권한 무변경·#678), #1087 매도측: `test_kis_lots_sell_evidence.py`(순수·90분 경계·KST 자정·clamp),
  `test_kis_lots_sell_evidence_db.py`(테스트 DB·KIS client/HTTP 트랩으로 무호출 단언·get_holdings end-to-end).
- **KIS live US 확장 (task #1173, 운영자 10-01)**: 같은 블록을 KIS live **US** 포지션(`market="us"`, `equity_us`)에도 붙인다 — `load_kis_live_us_lot_blocks` +
  `build_symbol_block(market="us")`. KR 블록은 바이트 동일(`test_kis_lots_us.py` 의 pre-change golden `kis_lots_kr_blocks_golden.json`), 기본 출력도 불변.
  - fill: `broker=kis`·`account_mode=live`·`instrument_type=equity_us`·`currency=USD`. authoritative 행의 venue 가 정확히 `NASD`/`NYSE`/`AMEX`(KIS 해외 주문 거래소 코드, 대소문자·공백만 정규화)가
    아니면(`NASDAQ`·`NAS`·`krx`·빈값 …) 세지 않고 블록을 `unknown`(`unrecognized_us_venue_rows`)으로 만든다 — 조용히 거르지 않는다. 심볼은 holdings 의 DB dot-format 키로
    `app/core/symbol.py` 변환(`BRK/B`·`BRK-B` → `BRK.B`)을 거쳐 맞춘다.
  - 자기 주문: `review.live_order_ledger`(`broker=kis`·`account_scope=kis_live`·`market=us`, ROB-407) + `review.kis_live_order_ledger` 의 `equity_us` 행(계약상 없어야 하지만 있으면 차단에 쓴다).
    증거 키 이름(`kis_live_order_ledger_open_*`)은 KR 과 동일하게 유지하고 출처는 `order_ledger_sources` 로 밝힌다.
  - 🔴 "당일" = **US 거래일**: 20:00 America/New_York(애프터 마감, DST 반영)에 넘어간다 — KST 자정 이후에도 그 US 거래일의 주문·체결은 차단한다. KIS 주간거래(10:00 KST = 전일 20:00/21:00 ET) 주문은 다음 US 거래일 소속.
  - freshness 는 같은 KIS reconcile run(한 run 이 `kr,us` 를 읽고 US fetch 오류는 run 전체를 실패시킴)을 쓴다. 브로커 수량 불일치는 항상 `unknown`. `external_orders_verifiable=false`·`scope` 문구 동일.
  - 격리 행(#1175)은 어느 뷰에도 안 들어간다(`execution_ledger_in_effect()`, `test_quarantine_readers_db::test_us_lots_drop_the_quarantined_row` 가 filter-dropped 뮤턴트까지 증명).
    아직 격리 안 된 접수통지 팬텀은 `websocket` 행이라 lot·순수량·수량대조·`sellable_by_ledger` 에 절대 안 들어가고, 당일 증거에서는 차단을 더할 뿐 줄이지 않는다.
  - 🔴 websocket 행의 supersede(authoritative 행이 같은 주문을 덮음)는 US 에서 **같은 US 거래일**일 때만 성립한다 — KIS 주문번호는 날짜를 넘어 재사용되므로
    과거 주문이 오늘 체결을 증거 뷰에서 지우지 못한다(tester r1 F1). 🔴 심볼 귀속은 **Python `_us_symbol_key`(`to_db_symbol(s.strip()).upper()`) 하나**로만 한다 — SQL 은 심볼을 비교·정규화·필터하지 않고(broker·mode·instrument_type·currency·격리·주문 원장 7일 창만), 읽은 행을 Python 에서 귀속한다. SQL 재구현이 혼합 구분자(r3 F3)·유니코드 대소문자(`ß`→`SS`, r4 B1)·유니코드 공백 패딩(r4 B2)에서 세 번 갈라졌기 때문이며, 정적 가드(`test_kis_lots_us_mutants.py`)가 로더의 SQL 심볼 표현을 금지한다. 체결 읽기는 FIFO 상 날짜 창이 없어 in-effect KIS live US 체결 전량을 읽는다. KR 키는 변경 전 그대로(golden 고정)이며 같은 잠재 문제는 별건 후속이다.
  - 🔴 US 블록은 **브로커 포지션마다 하나**다 — 로더가 포지션 순서의 리스트를 돌려주고 attach 가 인덱스로 붙인다(심볼 키 조회 금지). 정규화 키가 같은 포지션이 2개 이상이면(`BRK/B`·`BRK.B`) **전부** `unknown`(`duplicate_positions_for_symbol`)이고 known 은 붙지 않는다(tester r5 B3, 마지막 쓰기 승리 금지). KR 의 같은 패턴은 hk 1295 별건.
  - KR·US 는 별도 세션으로 읽어 한 시장 실패가 다른 시장 블록을 깎지 않는다. summary 는 `scope="kis_live_kr_us_positions"`·`positions_covered_by_market`, 비-live 라우팅은 `reason="kis_live_only"`.
  - 🔴 #678 `kis_live_get_order_history` 차단·harness deny 불변. us-open-trade 프롬프트 문장은 운영자 PR 별건.
  - **테스트**: `test_kis_lots_us.py`(순수·US 거래일·venue·팬텀·KR golden), `test_kis_lots_us_db.py`(테스트 DB·KIS/HTTP 트랩·get_holdings KR+US end-to-end),
    `test_kis_lots_us_mutants.py`(디스크에서 센 US 가드 12곳의 assertion-RED 뮤턴트).

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

### KIS mock legacy DAY 만료 분류 (Task 881, Q-46)

`kis_mock_ledger_expire_day_orders`는 hermes-paper-kis 전용 수동 도구다.
브로커 조회·변경 없이 로컬 주문 행, 체결 행, XKRX 달력만 읽는다. 전략·귀속 ID,
국내 현금주식 주문 경로의 양성 응답과 정확한 주문번호·시각, 일반 ORD_DVSN
00/01에 맞는 가격·주문형태, 0 체결 기록, N거래일 경과를 모두 증명해야 한다.
N의 기본값은 2다. 불명확하거나 이미 종료된 행은 쓰지 않는다. 확정 쓰기는
KISMockLifecycleService만 수행하며 별도 expired 상태와 제한된 감사 사유에
운영자 결정 참조와 규칙 버전을 보존한다. 두 번째 호출은 감사 횟수까지 순수
무변경이다. 브로커 미체결 증거로 해석하지 않는다. 실행 절차와 잔여 위험은
`docs/runbooks/kis-mock-reconciliation.md`의 #706 항목을 따른다.
일반 lifecycle 쓰기는 행 잠금과 fresh 재조회로 동시 만료를 확인하며, 만료된
행에서는 전이를 거부한다. 조정 작업은 해당 충돌을 이벤트 없는 행별 skip으로
기록한다.

### kis_mock 4행 Q-46 expired[inference] (#1250)

운영자 결정(hk #706 comment 1092, 10-05)으로 kis_mock 원장 80·66·64·63 **정확히
4행**에만 #1112 `expired[inference]` 규칙을 적용하는 일회성 레버다.

- **CLI**: `scripts/expire_kis_mock_rows_by_inference.py` — preview 기본, `--commit`,
  `--ids` 는 정확히 그 4개(부분집합·그 밖 id·중복·범위 거부, DB 연결 전), `--decision-ref`
  는 정확히 `Q-46`, `--reason`/`--actor` 필수, DB 는 `--database-url-env`(값 출력 금지)
- **규칙/쓰기**: `app/services/kis_mock_inference_expiry.py`(순수, 조건별 `_check_*`) →
  `kis_mock_inference_expiry_service.py`(사실 수집·FOR UPDATE 재판정) →
  `KISMockLifecycleService.close_rows_by_q46_inference`(허용 id·open 상태로 가드된
  UPDATE, 4행 아니면 전부 롤백). 브로커 호출·live 원장 읽기 0
- 🔴 **생략은 정확히 2개**: strategy 대조와 reconcile coverage(#1112
  `execution_ledger_covers_order_day` — kis_mock 에는 그 run 이 존재할 수 없음, director-1
  option A). preview 가 행마다 `waived_conditions` 를 출력하고 닫힌 행·감사 행에 caveat 와 함께
  기록한다. 생략은 4-id allowlist 안에서만 존재한다. 나머지 #1112 조건은
  kis_mock 증거로 번역해 전부 판정하고, 한 행이라도 실패하면 배치 전체 무변경:
  수락된 kis_mock KR 현금 BUY(native 응답 rt_cd 0·odno·ord_tmd 일치) · lifecycle
  accepted/pending · DAY(00/01) · 정규장 접수 · #1112 deadline 경과 · 그 주문의 체결 증거
  0(execution_ledger kis/mock 전 source·격리행 포함, 행 자체 체결 reason/수량, 같은
  correlation 행) · 보유 불변(`holdings_baseline_qty` 기록 + 접수 이후일 수 있는 종목 체결 0)
- **닫힌 행**: `expired` + `last_reconcile_detail.reason_code` = #1112 마커
  `expired_inference:kis_regular_day_order_no_broker_original`, `expiry_caveat=no_broker_original`,
  `operator_decision_ref=Q-46`. terminal 이라 open-order·예약·reconcile 리더에서 빠지고
  일반 전이 API 는 거부, #881 도구는 `already_terminal`
- **감사**: append-only `review.kis_mock_inference_expiry_events`(ledger_id UNIQUE, CHECK
  ledger_id ∈ {63,64,66,80}·decision ref Q-46, UPDATE/DELETE/TRUNCATE 트리거 거부).
  🔴 close↔audit 는 DB 가 결속: 같은 batch 로 닫힌 행이 아니면 감사 INSERT 거부, 마커가
  붙은 행은 COMMIT 시 같은 batch 감사 행 필수(deferred constraint trigger — 4 id 밖·
  accepted/pending→expired 외 전이·닫힌 행 재기록도 거부). id 는 정확히 built-in int 4개만
  (float/numpy/bool/str 거부). 쓰기 초크포인트가 배치를 스스로 재잠금·재판정하고, UPDATE 뒤
  모든 체결 소스를 다시 읽어 그 사이 커밋된 체결이면 전체 롤백. COMMIT 시점 deferred 트리거가 주문·종목·형제
  행 체결과 4행 배치를 SQL 로 재확인(재확인 이후 커밋된 체결도 전체 거부). 마커가 붙은 닫힌 행은
  UPDATE·DELETE 전부 거부(terminal — 마커를 지워 재오픈 불가)
  마이그레이션 `20261005_t1250_kismock_inf`(CREATE TABLE 만). 두 번째 commit 은 무변경 no-op
- **런북**: `docs/runbooks/kis-mock-expired-inference-q46.md`. 실DB 실행은 운영자 전용

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


### 7-D stale blocker night sweep + KIS expired[inference] (#1112)

7-D(종목당 활성 매수 1건)를 영구 차단하던 stale 행을 **기록만으로** 닫는다. 주문 생성·정정·취소·브로커 호출 0.

- **야간 스윕**: `order_proposal.night_sweep` (`app/tasks/order_proposal_expiry_tasks.py`) → `order_proposal_tools.run_order_proposal_night_sweep`.
  ① ROB-897 `sweep_expired` 를 **좁힌 범위**로 재사용 — group `proposed` 이고 모든 rung 이 `draft`/`pending_approval`/`needs_reconfirm` 인 `valid_until` 경과 제안만 `expired`, rung `void_reason=expired_valid_until_night_sweep`. `revalidating`/`approved`/제출 이후 rung 이 하나라도 있으면 skip. 범위 인자는 좁히기만 한다(voidable 집합과 교집합).
  ② KIS 잔존 rung 추론 종결(아래).
- 🔴 **스케줄 선언만, 등록 0**: cron `30 16 * * 1-5`·`0 7 * * 1-5`(Asia/Seoul)는 `ORDER_PROPOSAL_NIGHT_SWEEP_SCHEDULE_ENABLED`(기본 false)일 때만 import 시점에 라벨로 붙고, 본문은 `ORDER_PROPOSAL_NIGHT_SWEEP_ENABLED`(기본 false) 뒤. 활성화(플래그 + 스케줄러 재시작)는 desk 결정.
- **expired[inference]** (`app/services/order_proposals/kis_leftover_inference.py` 순수 규칙 + `kis_leftover_inference_service.py` I/O): `kis_live`/`equity_kr` BUY `resting` rung 을 브로커 원본 없이 `expired` 로 닫는 조건은 **전부**: 소유권 검증된 단일 open(`accepted`/`pending`) KIS 주문원장 행 · proposal·원장 모두 `limit`/`market`(DAY) · 송신·브로커 `ord_tmd` 가 XKRX 달력 정규장 안 + ROB-671 창 `regular` + 보고 venue 가 KRX/SOR(또는 미보고) · `now` 가 **max(접수일 15:30 KST, 달력 마감, ROB-671 기대만료)** 초과(기본 SOR 매수 = 20:00 → 16:30 스윕은 추론 안 함, 07:00 스윕이 닫음) · 성공·커밋된 KIS execution-ledger reconcile run 이 접수~deadline 을 덮고 deadline 이후 종료 · 그 주문의 fill(웹소켓 포함)·부분체결 없음 · 종목 원장 행 존재 + 접수 이후 종목 fill 0(순증감 0 이어도 차단). 하나라도 불만족/판독불가면 **차단 유지**. 적용은 group row lock 후 사실을 재조회·재판정해서만 쓴다.
- 🔴 **마커**: rung `void_reason=expired_inference:kis_regular_day_order_no_broker_original`(group `cancelled_or_expired`). `order_proposal_get/list` rung 투영에 `expiry_basis="inference"`·`expiry_caveat="no_broker_original"` 가 이 마커일 때만 붙는다. `review.kis_live_order_ledger`·`execution_ledger` 는 **쓰지 않는다**(브로커 증거 원장 불변). 추론 후 실체결이 오면 원장은 정상 기록되고 rung 은 terminal 이라 투영되지 않는다 — 마커로 감사.
- **7-D 차단 사유**: `order_proposal_list(include_kr_buy_blocking=true)`(기본 false·기본 출력 불변) → `kr_buy_blocking`: 비터미널 KR buy 제안 rung 마다 `proposal_id`·`rung_id`·`rule`(`nonterminal_buy_proposal`/`stale_proposal_past_valid_until`/`kis_resting_rung_inference_conditions_not_met`/`kis_resting_rung_inference_eligible_pending_sweep`/`broker_live_rung_awaiting_broker_evidence`) + KIS resting 은 조건별 판정, 그리고 최근 7일 스윕/추론으로 닫힌 행(`basis`·`cleared_at`·`caveat`). 읽기 시점 제외 없음 — DB 에 terminal 로 써진 행만 빠진다. 범위는 제안 행뿐(Toss 원본·KIS `open_buy_evidence` 는 별도 입력, 부재 증명 아님). 판독 실패는 `state="unknown"`.
- **알려진 한계**: 라이브러리 XKRX 달력은 수능일 16:30 마감을 모델링하지 않는다 — 기본 SOR 매수는 20:00 deadline 이라 무관. deadline 이 15:30 으로 내려가는 두 경우(브로커 venue 가 `KRX` 로 보고된 주문, 또는 `KIS_REGULAR_BUY_UNSETTLED_EXPIRY_1530` 플래그가 켜진 SOR 매수)에만 수능일 15:30–16:30 잔여가 남는다. 적용은 07:00 스윕이면 무관, 16:30 스윕에서 그 날 마감 직후 한 차례만 해당.
- **7-D 카운트 범위**: lifecycle 비터미널 group **또는** 비터미널 rung 을 하나라도 가진 group(예: `superseded` 뒤에도 남은 `acked`/`resting` 매수)을 센다.
- **테스트**: `tests/services/order_proposals/test_kis_leftover_inference.py`(조건별 단독 실패), `test_kis_leftover_inference_mutants.py`(디스크 카운트 mutant + 주문경로 import 가드), `test_night_sweep_unit.py`, `test_night_sweep_db.py`(A1 브로커 트랩·A5 멱등).

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

### NHPLUG Mock Dispatch State Machine (Stage 2)

The Stage 1 account, balance, and quote reads remain on the pinned mock data host. Stage 2 adds a single order send owner in app/services/brokers/nhplug/client.py, a service-owned review.nhplug_mock_order_ledger, and manual order-list reconciliation. The full transition and evidence specification is docs/design/711-nhplug-dispatch-state-machine.md.

- **Host and account**: all data and order requests use only HTTPS moapi.nhplug.com:8443. The same client must read /n2/acctinfo and establish an acct_type=03 allowlist before dispatch. A caller-supplied allowlist enables reads only. The built request is checked again for scheme, host, port, exact order path, account number, and body before the T5 fence and send. OAuth token/revoke remain the only api.nhplug.com:8443 exception in nhplug/auth.py. Redirects and lower-layer retries are disabled.
- **Default-disabled release**: NHPLUG_MOCK_ENABLED and NHPLUG_STAGE2_KEY_CONFIRMED, TIME_CONFIRMED, DB_CONFIRMED, HOST_CONFIRMED, VENDOR_CONFIRMED are all exact true gates and default false. The key and time values use configured defaults, but their confirmations are explicit. B-DB durability, B-HOST process visibility, and B-VENDOR order-number and listing premises must be established by the operator before enabling. No live account, market order, schedule, or production-host data request is allowed.
- **One send per claim**: an operator or caller supplies a stable idempotency key. T1 records the immutable intent and date-independent in-flight reservation; T3 claims it conditionally; T5 commits a sending fence before the first possible application write. The body is rebuilt solely from the claimed row and compared against its database digest. A missing fence or pre-send refusal cannot send. Once fenced, every ambiguous result, exception, timeout, or process death remains sending until manual recovery makes it uncertain. A result-recording failure leaves exactly sending immediately; it never permits a second send.
- **No unsupported acceptance or rejection**: success-proof and no-order-proof code tables start empty. A response with an order number but no documented success code records uncertain and preserves the number. The sending-to-rejected transition requires a DB-listed no-order proof code, so it is unreachable while the table is empty. An uncertain row retains its reservation across trading days. The DB refuses uncertain-to-anomaly without its own number and a positive conflicting listing row; it refuses arbitrary reservation release.
- **Manual evidence and authority**: a complete all-orders listing containing the same acknowledged number and matching order attributes may promote uncertain to accepted. Other similar orders are candidates only. Filled needs a second filled-scope confirmation; cancelled and modified need our own acknowledged request and positive original-order evidence. Operator candidate binding and unresolved-risk abandonment consume a matching, one-use authorization row. The application role has SELECT only on authorization, code, and key-version tables. T14 also requires the lease-host process-death witness, elapsed grace, a complete listing, and explicit operator risk acceptance. Unknown or partial listings never prove absence.
- **B-HOST deployment (#1046)**: only the DEFAULT MCP blue/green pair mounts the host `/etc/machine-id` read-only. A lease-identity failure after T1 withdraws only the unclaimed intent; no broker send occurs, and the same-key replay rule remains intact. A read-only, one-shot T14 witness runs from the deployed image digest with `--pid=host` and no network. Different-namespace death proof fails closed: it needs the scanner in the kernel initial PID namespace, equal to the inode measured on the host, and an exact read of every `/proc/*/ns/pid` link; a sibling container, a different namespace, or one unreadable link is neither verified nor proven. Under default container security other users' links are unreadable, so that proof stays unavailable until the operator decides witness privileges. No T14 transition CLI exists, and the witness alone never releases an uncertain reservation. See `docs/runbooks/nhplug-mock-smoke.md`.
- **Identity and uniqueness**: all retained key versions are checked before account binding writes. The account reference stays stable across rotation. The order-number UNIQUE premise is exactly account reference plus trading day plus broker order number; this vendor premise remains B-VENDOR until confirmed. Intent, claim, fence, and evidence columns are protected by DB constraints and transition triggers. Service code is the only normal ledger writer.
- **MCP surface (#849)**: `app/mcp_server/tooling/orders_nh_mock_variants.py` declares nine `nh_mock_*` tools (preview/place/modify/cancel, order history/detail, positions, orderable cash, reconcile). They register only in the DEFAULT profile when `NH_MOCK_MCP_ENABLED=true` (default false); no live profile, lane allowlist, or TradingCodex profile lists them. Every check lives in `app/services/nhplug_mock/operations.py` (design actor O): limit-only grammar (the exact string "limit" plus a positive integer price) before any network or DB access, exact `dry_run`/`confirm`, a required idempotency key, all Stage 2 gates, credentials by key name, the retained key registry, and a fresh `/n2/acctinfo` acct_type=03 check before T1. Modify and cancel need a same-day place/modify row this ledger dispatched whose broker number is bound, plus a complete all-scope listing that still shows the order open; `amend_scope` comes from that listing. The one send site is `_dispatch_new_intent` calling the #711 dispatcher, and the caller answer is read back from the durable row. History, detail, and reconcile report empty or incomplete listings as unknown. `nh_mock_reconcile_orders` defaults to a read-only dry run; its confirmed run does V recovery, T9b, candidate recording, and T11/T12 through the ledger service and never sends. Reconcile answers `reconciled` only when every targeted row (sending/uncertain plus bound rows) is resolved; an incomplete scope or unverified bound row is `unknown`, otherwise any row left sending/uncertain is `partial` (some resolved) or `uncertain` (none); with none unresolved, an `anomaly` or `requires_manual_review` row is `needs_review` and is never counted as resolved; all with `success=false` and the rows named (#942). The dry run predicts the same words in `would_be_status` and says `verification_pending`, never `reconciled`, while bound or listed rows still need the confirmed ledger checks. Order detail on an incomplete all-scope listing is `success=false`, `status=unknown`, `reason=order_listing_incomplete`. Static guard and mutant tests: `tests/services/brokers/nhplug/test_static_guard.py`, `tests/services/nhplug_mock/test_nh_mock_mutants.py`, `tests/services/nhplug_mock/test_nh_mock_status_honesty.py`.
- **Existing Stage 1 boundary**: the vendor nhplug SDK and NHPLUG_BASE_URL/NHPLUG_AUTH_URL overrides remain forbidden. The old read smoke is still limited to its operator-created three-key file, and the live same-key risk remains. No real order smoke is run by builders or testers; operator-desk performs it after merge.

### 토스증권 Open API (ROB-529)

토스증권 Open API(`https://openapi.tossinvest.com`, OAuth2 Client Credentials, REST-only) 기반 KR/US **live** 브로커 + 시세·종목마스터·환율·캘린더 데이터 소스. 모의투자 없음(live 단일).

- **파킹 제안 계좌 (#765)**: proposer는 read-only `toss_proposal_accounts`의 broker-listed account sequence 중 의도한 하나를 top-level `broker_account_id`로 명시한다. 설정·rationale·이전 제안에서 자동 채우지 않는다. Toss 파킹 meter는 명시값이 설정된 holdings reader의 sequence와 정확히 같고 broker account list에 정확히 1건 존재한 뒤에만 holdings를 읽는다. NULL/비정규형은 `account_identity_unavailable`, 다른 설정 계좌는 `account_identity_mismatch`, broker list 부재/중복은 `account_identity_unknown`, 계좌 조회 실패는 `account_lookup_failed`로 수동 카드다. 다계좌도 암묵 선택하지 않는다. 비파킹 제안은 이 검사 밖이다.

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
