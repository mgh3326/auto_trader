# ROB-475 — 체결 자동 정산 (auto-booking) 설계

- **Linear**: ROB-475 `[DX] 체결 자동 정산 — 주문마다 수동 reconcile 의존 제거 (auto-booking)`
- **Priority**: Medium
- **날짜**: 2026-06-09
- **선행**: ROB-395 (KIS Live Order Fill-Evidence Gate)
- **PR**: 단독 PR (ROB-476과 분리)

## 문제

`kis_live_place_order(dry_run=False)`는 주문을 **accepted-only**로만 기록한다
(`fill_recorded:false`, `journal_created:false`). 체결을 로컬 장부
(trade/journal/realized_pnl)에 남기려면 운영자가 **매번 수동으로**
`kis_live_reconcile_orders(dry_run=False)`를 돌려야 한다. 반복 chore이고, 잊으면
거래내역·실현손익이 시스템에 안 남아 리포트·성과추적이 불완전해진다. 운영자가
"이걸 왜 매번 해야 하나"를 직접 질문(2026-06-09 라이브).

**설계 제약**: ROB-395의 accepted-only 게이트는 의도된 설계다 — 체결 증거 없이
선반영하지 않는다. 이 설계는 그 게이트를 **유지**하되, 증거-게이트 reconcile을
운영자가 손으로 돌리는 대신 **주기적으로 자동 실행**한다.

## 접근 (결정됨)

**Scheduleless 자동 reconcile 태스크 + 도구 설명 명확화.**

새 booking 로직은 없다. 이미 검증된 reconcile 커널
(`kis_live_reconcile_orders_impl`, `kis_live_ledger.py:522`)을 주기 TaskIQ
태스크가 `dry_run=False`로 호출한다. 기본 비활성(default-off) env 게이트 →
운영자가 활성화하기 전까지 inert. 이는 ROB-404
(`kis_mock_reconciliation_tasks.py`)·ROB-405·ROB-337 Slice2의 기존 패턴과 동일.

대안 검토:
- **(채택) Scheduleless 자동 reconcile 태스크** — 리포 패턴과 일치, 실질 해결,
  활성화는 operator-gated. reconcile 커널 재사용 → 새 mutation 경로 없음.
- (반려) 도구 설명만 추가 — chore를 해소 못함. 단, 이 설계는 설명 명확화도
  **함께** 포함한다(아래 컴포넌트 4).
- (반려) place_order 인라인 track-to-fill — KR 지정가는 분~시간 후 체결되므로
  동기 폴링은 resting limit에 무의미.

## 컴포넌트

### 1. Reconcile 커널 재사용 (변경 거의 없음)
`kis_live_reconcile_orders_impl(*, dry_run=...)` (`kis_live_ledger.py:522`)는 이미
MCP plumbing 없이 호출 가능한 plain async 함수다. 태스크가 직접 호출한다.
`_list_open_ledger_rows` → `_reconcile_one_ledger_row` 경로 무변경.

> 참고: ROB-476이 먼저 머지되면 이 커널이 expired 해소까지 자동 수행한다(시너지).
> 두 PR은 독립적으로 머지 가능하며 순서 무관.

### 2. 새 TaskIQ 태스크 — `app/tasks/kis_live_reconcile_tasks.py`
`kis_mock_reconciliation_tasks.py`를 그대로 미러:

```python
@broker.task(task_name="kis_live.reconcile_periodic")  # no schedule → paused
async def kis_live_reconcile_periodic() -> dict:
    if not settings.KIS_LIVE_AUTO_RECONCILE_ENABLED:
        return {"status": "paused",
                "message": "KIS_LIVE_AUTO_RECONCILE_ENABLED is False"}
    return await kis_live_reconcile_orders_impl(dry_run=False)
```

- **스케줄 없음**: 태스크만 등록, cron은 이 리포에 없음. 운영자가
  `robin-prefect-automations`에서 cron 추가 + 플래그 flip.
- **기본 비활성**: `settings.KIS_LIVE_AUTO_RECONCILE_ENABLED` (default `False`).

### 3. Operator CLI — `scripts/kis_live_auto_reconcile.py`
운영자가 온디맨드로 돌리거나 cron에 연결할 수 있는 얇은 wrapper.
- `--dry-run` (default True) → `kis_live_reconcile_orders_impl(dry_run=...)`.
- 결과 counts/summary를 표준출력으로 보고. secrets 출력 없음.

### 4. 도구 설명 + 런북 명확화 (제안 #2)
- `kis_live_place_order` 설명: "체결은 즉시 기록되지 않음. 자동 정산
  태스크(`kis_live.reconcile_periodic`, operator-gated)가 켜져 있으면 주기적으로
  체결을 기록함. **reconcile은 로컬 부기 레이어이며, 실계좌 진실은
  `get_holdings`/`get_available_capital`**."
- `kis_live_reconcile_orders` 설명: 위 "로컬 부기 레이어 vs 실계좌 진실" 문구 추가.
- `docs/runbooks/kis-live-order-reconcile.md`: 자동 정산 태스크 활성화 절차
  (env 플래그 + cron은 robin-prefect-automations) 섹션 추가.

### 5. Config — `app/core/config.py`
`KIS_LIVE_AUTO_RECONCILE_ENABLED: bool = False` 추가 (기존 `KIS_MOCK_RECONCILE_
PERIODIC_ENABLED` 옆).

## 데이터 흐름

```
operator/cron (외부, robin-prefect-automations)
  └─> kis_live.reconcile_periodic (paused until ENABLED=true)
        └─> kis_live_reconcile_orders_impl(dry_run=False)   # 기존 커널
              └─> _list_open_ledger_rows()  (accepted|pending|partial)
                    └─> _reconcile_one_ledger_row()  # 증거-게이트 booking
                          └─> trade/journal/realized_pnl 기록 + ledger status 갱신
```

place_order 경로(accepted-only)는 **무변경**. 게이트 유지.

## 안전 경계

- **kis_live KR 한정** (커널 스코프와 동일).
- **Default-off inert**: 플래그 미설정 시 `{"status":"paused"}` 반환, 부수효과 0.
- **새 mutation 경로 없음**: 검증된 reconcile 커널만 호출.
- **스케줄 비등록**: TaskIQ 태스크만, cron 연결은 operator.
- **Migration: 0**.

## 테스트

- 플래그 False → 태스크가 `{"status":"paused"}` 반환, 커널 미호출(mock으로 단언).
- 플래그 True → 커널을 `dry_run=False`로 1회 호출(mock), 결과 패스스루.
- CLI dry-run 기본값 → 커널을 `dry_run=True`로 호출.
- 라이브 호출 없음 (broker 전부 mock).

## 미해결/후속

- 실 cron 등록 + 플래그 flip = operator (robin-prefect-automations), 이 PR 밖.
- 이벤트-드리븐(체결 webhook) 자동 booking은 KIS live webhook 부재로 범위 외 —
  주기 폴링이 현실적 수단.
