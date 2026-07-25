# Live Order Ledger Periodic Reconcile Runbook (ROB-1050)

**Last Updated:** 2026-07-25  
**Scope:** US 해외주식 (`equity_us`) & Upbit Crypto (`crypto`) live order ledger periodic reconcile tasks, backfill CLI, and observation metrics.

---

## 1. 개요 및 환경 변수 게이트 (Env Gates)

본 저장소의 periodic live reconcile 태스크는 **scheduleless**(코드 내 `schedule=` 라벨 없음) 상태로 출고되며, 2단계 디폴트-Off 안전 게이트로 보호된다.

### 환경 변수 게이트 2종의 의미

| 환경 변수 | 기본값 | 설명 |
|---|---|---|
| `LIVE_AUTO_RECONCILE_ENABLED` | `False` | **전역 마스터 스위치.** `False`일 경우 태스크는 DB/브로커 조회를 수행하지 않고 즉시 `{"skipped": "disabled"}`를 반환함. |
| `LIVE_AUTO_RECONCILE_DRY_RUN` | `True` | **안전 스캔 플래그.** `LIVE_AUTO_RECONCILE_ENABLED=True` 상태에서도 기본값 `True`로 동작하여, 브로커 체결 증거만 수집/보고하고 DB 쓰기(Fill/Journal 기장)를 수행하지 않음. |

### 켜는 순서 (Activation Sequence)

1. **1단계 (안전 증거 스캔):**
   - `LIVE_AUTO_RECONCILE_ENABLED=True`
   - `LIVE_AUTO_RECONCILE_DRY_RUN=True` (기본값 유지)
   - 태스크 실행 후 반환 dict의 `counts` 및 `verdict` 결과를 확인하여 브로커 조회 증거가 정합성을 만족하는지 점검.
2. **2단계 (실체 기장 활성화):**
   - 1단계 dry-run 검증 완료 후 `LIVE_AUTO_RECONCILE_DRY_RUN=False`로 변경하여 증거 기반 체결/만료 확정 자동화 적용.

---

## 2. 적체 주문 백필 절차 (Backfill Procedure)

적체된 미확정 live 주문 건을 안전하게 해소하기 위한 4단계 절차:

### ① 1단계: 백필 현황 리포트 CLI 조회 (읽기 전용)
```bash
uv run python scripts/live_reconcile_backfill_report.py --market all
```
- **목적:** 적체 행의 시장별/브로커별 distribution 및 브로커 API 조회 가능 기간(KIS 90일, Upbit UUID 조회 가능) 내 포함 여부 확인.

### ② 2단계: Dry-Run Reconcile 증거 수집
```bash
LIVE_AUTO_RECONCILE_ENABLED=true LIVE_AUTO_RECONCILE_DRY_RUN=true \
  uv run python -c "import asyncio; from app.tasks.live_reconcile_tasks import live_reconcile_us_periodic; print(asyncio.run(live_reconcile_us_periodic()))"
```
- **목적:** 브로커 API 조회를 통해 `would_book_filled`, `would_mark_expired`, `would_mark_cancelled`, `noop_pending` 분류 결과를 미리 확인 (DB Mutation 0).

### ③ 3단계: 백필 실행 (Non-Dry Reconcile)
운영자 확정 후 수동 실행 또는 MCP `live_reconcile_orders` 도구를 통해 `dry_run=False`로 실행.

### ④ 4단계: 결과 검증
```bash
uv run python scripts/live_reconcile_backfill_report.py
```
- `reconciled_at IS NULL` 미확정 행 수가 해소되었는지 (또는 KIS 90일 경과 `noop_pending` 건만 남았는지) 확인.

---

## 3. 권장 주기 및 스케줄 등록 안내

- **스케줄 등록 주체:** 코드 내에는 cron/schedule 라벨이 존재하지 않으며, **스케줄 등록은 전적으로 운영자 몫**이다.
- **권장 주기:** **시간당 1회 (Hourly,예: `0 * * * *`)**
- TaskIQ scheduler 또는 Prefect 등 외부 스케줄러 등록 시, `live.reconcile_us_periodic` 및 `live.reconcile_crypto_periodic` 태스크 이름을 사용하여 1회 호출 단위로 주기 배치 등록 가능.

---

## 4. 크립토 웹소켓 체결 감지 장애 관련 주의사항

- **주요 장애 사실:** Upbit 크립토 웹소켓 실시간 체결 감지 기능(`upbit_websocket_monitor.py`)은 **2026-05-14 이후 무응답(stale) 상태**이다.
- **주기 Reconcile의 역할:** 웹소켓 감지가 복구되기 전까지, 주기적 Reconcile 태스크(`live.reconcile_crypto_periodic`)가 Crypto live 주문 체결 확정 및 trade journal 기장을 담당하는 **유일한 1차 방어선**이다.
- 웹소켓 복구 전까지 Crypto 주기 reconcile 태스크가 정상 가동되는지 유의 깊게 관측해야 한다.
