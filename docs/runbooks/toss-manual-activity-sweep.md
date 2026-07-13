# Toss 수동 거래 감지 스윕 (ROB-866)

토스는 체결 웹소켓이 없어 운영자의 앱 수동 매매(직접 주문)를 시스템이 인지하지
못한다(KIS/Upbit는 웹소켓 fill-트리아지가 자동 기동). 이 스윕은 토스 `GET
/api/v1/orders`(CLOSED 페이지네이션 + OPEN 1콜)를 `review.toss_live_order_ledger`
및 order_proposal rung `broker_order_id`와 대조해 **원장에 없는 = 수동 거래**를
발견하고, 실행 모드에서 텔레그램 알림 + `session_context` 핸드오프를 남긴다.

**1단계 스코프**: 감지 + 알림만. 자동 저널/체결 부기는 없다(2단계). 브로커 대상
호출은 read-only(`list_orders`만).

## 구성

- **감지 커널**: `app/services/toss_manual_activity.py`
  - `detect_manual_activity(...)` — 순수 감지(원장/proposal 대조).
  - `run_manual_activity_sweep(...)` — DB/텔레그램/session_context 배선 + 멱등 필터.
- **MCP 도구**: `toss_detect_manual_activity(window_hours=24, dry_run=True)`
  (`app/mcp_server/tooling/toss_manual_activity_tools.py`).
- **멱등 마커**: `review.toss_manual_activity_alerts` (broker_order_id PK).
  이미 알린 주문 재알림 방지 전용 — **체결 원장 아님**.
- **TaskIQ**: `toss.manual_activity_sweep` (scheduleless, `app/tasks/toss_manual_activity_tasks.py`).
- **마이그레이션**: `20260713_rob866_toss_manual_activity` (additive).

## 안전 경계 / env 게이트

- 읽기는 `TOSS_API_ENABLED=true` + client id/secret 필요 (`validate_toss_api_config`).
  미설정 시 MCP 도구는 `success=false` + `missing_env` 반환(네트워크 호출 없음).
- `TOSS_MANUAL_ACTIVITY_SWEEP_ENABLED`(기본 false) — **auto-run TaskIQ task**만 arm.
  MCP 도구(operator 직접 호출)는 이 플래그와 무관하며 dry_run으로 게이트.
- 브로커 mutation 0. 멱등 마커/session_context 외 쓰기 없음.
- 스케줄 연결 없음 — recurrence는 수동 reps 후 별도 결정(operator/Prefect 등록).

## 수동 플레이북 (MCP)

1. **발견만 (쓰기 0)**: `toss_detect_manual_activity(window_hours=24, dry_run=True)`
   → `manual_filled`/`manual_open` 목록 확인. `new_count`는 아직 알리지 않은 건수.
2. **알림 + 핸드오프**: `toss_detect_manual_activity(window_hours=24, dry_run=False)`
   → 신규 수동 주문마다 텔레그램 발송 + 해당 market(kr/us) `session_context`
   handoff_note 기록 + 멱등 마커 저장. 같은 주문은 다음 스윕에서 재알림 안 됨.
3. **부기**: 알림은 "부기 필요" 신호일 뿐 — 실제 저널/체결 반영은 운영자/2단계가
   수행(원장에 편입되면 이후 스윕에서 자동으로 오탐 0).

## 활성화(auto-run) — 연기됨

`TOSS_MANUAL_ACTIVITY_SWEEP_ENABLED=true` + operator/Prefect가 `toss.manual_activity_sweep`
cadence 등록 시에만 자동 실행. 수동 reps로 감지 신뢰 확보 후 결정한다.
