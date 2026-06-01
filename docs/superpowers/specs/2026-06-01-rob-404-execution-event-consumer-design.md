# ROB-404 — Redis `execution:{market}` consumer → 즉시 mock reconcile

- **이슈**: ROB-404 (G3) — Redis `execution:{market}` 체결 이벤트 소비자 → 즉시 mock reconcile + 자동 스케줄
- **부모 에픽**: ROB-401 (모의 자율매매 루프), 오케스트레이션 트래커 ROB-410 Wave 1
- **선행**: ROB-400 (Done) — delta-budget attribution 커널
- **작성일**: 2026-06-01
- **상태**: 설계 승인됨 → 구현 계획 수립 단계

## 1. 배경 / 문제

`kis_websocket_monitor.py`(mock_mode)가 kis_mock 체결을 Redis `execution:{market}` 채널로 이미 발행하지만, 이를 소비해 정산하는 consumer가 없다 → reconcile은 수동(MCP)/폴링뿐. 체결 즉시 평단·roundtrip PnL을 확정하지 못해 회고(ROB-405) 데이터 신선도가 떨어진다.

### 현 상태 (코드 매핑)

- **발행**: `app/services/execution_event.py::publish_execution_event(event)` → 채널 `execution:{market}`(`market∈{kr,us}`). 페이로드: `order_id`, `symbol`, `side`, `market`, `filled_qty`, `filled_price`, `correlation_id`(UUID4.hex 자동), `account_mode`(`kis_mock`|`kis_live`), `broker`(`kis`), `execution_source`(`websocket`), `execution_type`, `fill_yn`. 유일 발행처 = `kis_websocket_monitor.py::_on_execution`.
- **Redis 클라이언트**: `execution_event._get_redis_client()` (지연 초기화, `decode_responses=True`).
- **Reconcile**: `app/jobs/kis_mock_reconciliation_job.py::run_kis_mock_reconciliation(db, *, dry_run=True, limit=100, thresholds=None, kis_client=None)` — **배치 전용**(symbol 필터 없음). 내부 `KISMockLifecycleService.list_open_orders(limit=...)`는 **이미 `symbol=` 인자 지원**. ROB-400 `classify_orders`/`classify_fill_by_delta`(delta-budget) 커널을 거치는 **유일 경로**.
- **세션**: `AsyncSessionLocal()` 컨텍스트(MCP `kis_mock_ledger.py`가 사용).
- **기존 Redis pubsub consumer 없음** — ROB-404가 최초. `kis_websocket_monitor` asyncio 루프 패턴 미러.
- **taskiq**: `app/core/taskiq_broker.py::broker`. `@broker.task(task_name=...)` + `schedule` 생략 = paused. env flag(default False) 게이트 관례(`RESEARCH_REPORTS_INGEST_COMMIT_ENABLED` 등).
- **correlation_id**: 이벤트에 포함, `KISMockOrderLedger.correlation_id` 인덱스 존재. 기존 멱등/디듀프 헬퍼는 없음.

## 2. 목표 / 비목표

**목표**
- kis_mock 체결 이벤트 수신 시 해당 symbol을 **즉시 reconcile**(이벤트 구동).
- **주기 reconcile 폴백**(taskiq, paused).
- 전부 **default-off/paused**, **correlation_id 멱등**, **ROB-400 attribution 커널 재사용**(새 매칭 로직 없음).

**비목표 (범위 제외)**
- 주기 태스크의 실제 cron schedule 등록(operator flip + 별도 PR).
- live execution consumer / live 자동 reconcile (kis_live는 영구 무시).
- reconcile 커널 변경, 새 fill-matching 로직.
- 스키마/마이그레이션 변경 (없음).

## 3. 설계

### 3.1 Job 확장 — per-symbol reconcile

`run_kis_mock_reconciliation(db, *, dry_run=True, limit=100, symbol=None, thresholds=None, kis_client=None)`:
- `symbol`을 `list_open_orders(limit=limit, symbol=symbol)`로 전달. `None`이면 기존 배치 동작.
- delta-budget은 symbol+side cohort 단위라 단일 symbol reconcile이 정합(그 symbol의 모든 open 주문 포함). **커널·반환구조 무변경**.

### 3.2 Consumer — `app/services/kis_mock_execution_consumer.py`

`KISMockExecutionConsumer`:
- `execution:*` **psubscribe**(kr+us 커버) → 메시지 JSON 파싱(`json.loads`).
- **하드 필터**(통과 못하면 무시): `account_mode == "kis_mock"` AND `broker == "kis"` AND 체결 이벤트(`fill_yn == "Y"` 또는 `execution_type`가 체결). live/ack/reject/비-mock은 무시.
- **멱등**: `redis.set(f"kis_mock:exec_processed:{correlation_id}", "1", nx=True, ex=3600)`. False(이미 존재)면 skip. `correlation_id` 없으면 skip + warning(멱등 보장 불가한 이벤트는 처리 안 함, fail-closed).
- **gate**: `KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED`(default False)면 `dry_run=True`(read-only preflight), True면 `dry_run=False`.
- `async with AsyncSessionLocal() as db: await run_kis_mock_reconciliation(db, symbol=event["symbol"], dry_run=<gated>)`.
- asyncio 루프 + signal(SIGINT/SIGTERM) graceful shutdown, pubsub 자원 정리. monitor 패턴 미러. 자체 redis pubsub 연결(`_get_redis_client()` 재사용).

### 3.3 CLI — `scripts/kis_mock_execution_consumer.py`

operator-run, default-disabled. 모드: `preflight`(gate 무관 dry_run 강제, 구독·필터·디듀프만 검증) / `run`(gate에 따름). 기존 smoke CLI 미러(missing env 이름만 보고, 값 출력 없음).

### 3.4 taskiq 주기 태스크 — `app/tasks/kis_mock_reconciliation_tasks.py`

```python
@broker.task(task_name="kis_mock.reconcile_periodic")  # schedule 없음 → paused
async def kis_mock_reconcile_periodic() -> dict[str, object]:
    if not settings.KIS_MOCK_RECONCILE_PERIODIC_ENABLED:
        return {"status": "paused", ...}
    async with AsyncSessionLocal() as db:
        return await run_kis_mock_reconciliation(db, dry_run=False)
```

### 3.5 Config — `app/core/config.py`

```python
KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED: bool = False
KIS_MOCK_RECONCILE_PERIODIC_ENABLED: bool = False
```

## 4. 컴포넌트 / 인터페이스

| 단위 | 위치 | 책임 |
|---|---|---|
| Job symbol 필터 | `app/jobs/kis_mock_reconciliation_job.py` | `symbol` 인자 추가, `list_open_orders`로 전달 |
| Consumer | `app/services/kis_mock_execution_consumer.py` | psubscribe → 필터 → 멱등 → gated reconcile. 단일 책임. |
| CLI | `scripts/kis_mock_execution_consumer.py` | operator preflight/run 진입점 |
| 주기 태스크 | `app/tasks/kis_mock_reconciliation_tasks.py` | paused taskiq task, env-gated |
| Config | `app/core/config.py` | 2개 default-False 플래그 |

## 5. 데이터 모델 변경

**없음.** 스키마/마이그레이션 변경 0. (멱등 키는 Redis 휘발성.)

## 6. 안전 경계

- **kis_mock-only 하드 필터**: `account_mode != "kis_mock"`(특히 `kis_live`) 또는 `broker != "kis"`면 무시. live 체결이 mock reconcile을 트리거 절대 불가.
- **default-off**: consumer reconcile mutation은 `..._ON_EXECUTION_ENABLED` True일 때만(기본 dry-run preflight). 주기 태스크는 schedule 없음(paused) + `..._PERIODIC_ENABLED` 게이트.
- **멱등**: correlation_id SETNX(TTL) 디듀프 + reconcile 자체 idempotent(delta-budget). 이벤트 폭주/중복 reconcile 차단(검토 갭 3).
- **스케줄러 auto-start 없음**: operator CLI + paused taskiq. production env/secret 변경 없음. secret 출력 없음.
- broker mutation은 kis_mock 한정.

## 7. 테스트

1. job `symbol` 필터: `symbol=X`면 `list_open_orders(symbol=X)` 호출, X cohort만 reconcile. `symbol=None`이면 기존 배치.
2. consumer: kis_mock fill 이벤트 → `run_kis_mock_reconciliation(symbol=event_symbol, ...)` 호출(job mock).
3. consumer 무시: `account_mode="kis_live"` / `broker!="kis"` / 비-fill(`fill_yn="N"`/ack) → reconcile 미호출.
4. 멱등: 동일 `correlation_id` 2회 → reconcile 1회만(SETNX). `correlation_id` 없음 → skip + warning.
5. gate: `..._ON_EXECUTION_ENABLED` False → `dry_run=True`로 호출; True → `dry_run=False`.
6. 주기 태스크: `..._PERIODIC_ENABLED` False → `{"status":"paused"}` + reconcile 미호출; True → job 호출.
7. CLI: `--help`/preflight가 secret 없이 동작(lazy import), 값 출력 없음.

## 8. 미해결 / 후속

- 주기 태스크 cron schedule 등록 + `..._PERIODIC_ENABLED` flip(operator, 별도 PR).
- operator live-mock smoke: 실제 kis_mock WS 발행 → consumer reconcile 라운드트립(creds 필요, 이 PR 미수행).
- ROB-405(회고 배선)가 이벤트/reconcile 결과 위에 구축.
- 동기 confirm-path(`holdings_delta_confirm.py`)의 ROB-404 NOTE된 잠재버그 — 이벤트 구동 path 정착 후 별도 점검.
