# Mock 자율매매 루프 활성화 런북 (ROB-401 / ROB-410)

ROB-410(Wave 1+2+3)으로 mock(kis_mock) 자율매매 루프의 **코드는 main에 완성**됐다. 모든 컴포넌트는 **default-off + paused**라 배포만으로는 아무 동작도 하지 않는다(inert). 이 문서는 operator가 루프를 단계적으로 활성화하는 절차다.

> **안전 원칙**
> - **live 자동집행은 영구 차단**(코드 가드 `AutoExecuteLiveBlocked` + executor `is_mock=True` 하드핀 + `watch_order_intent_ledger.account_mode='kis_mock'` DB CHECK). 어떤 flag로도 live 주문은 나가지 않는다.
> - broker mutation은 **kis_mock 한정**. 모든 쓰기는 멱등(correlation_id).
> - 활성화는 **관측 → 기록 → 집행** 순서로. 집행(ROB-402)은 **가장 마지막**.
> - 각 단계 후 evidence를 ROB-401/410에 남긴다.

## 0. 사전 조건
- main 배포(ROB-410 9 PR + #1207 TaskIQ 등록 포함).
- kis_mock 자격증명: `KIS_MOCK_ENABLED=true` + `KIS_MOCK_APP_KEY` / `KIS_MOCK_APP_SECRET` / `KIS_MOCK_ACCOUNT_NO` (값 출력 금지).
- Redis(execution 이벤트/디듀프), TaskIQ broker 가동.

## 1. 마이그레이션
```bash
uv run alembic upgrade head
```
포함 변경(전부 additive): watch_condition `conditions/combine/threshold_high`+operator `between`(403), watch_events operator/threshold_high(403), kis_mock lifecycle `cancelled`(406), action_mode `auto_execute_mock`(402), trade_journals `correlation_id`+account_type `mock`(405A), `trade_journal_reviews`(405B), `trade_journal_counterfactuals`(405C). 404/405D/405E는 마이그레이션 없음.

## 2. Flag flip (모두 default `False` → `True`)
**권장 순서 = 관측 → 기록 → 집행.** 각 flip 후 관찰 → 이상 없으면 다음.

| 순서 | 컴포넌트 | env 변수 | 효과 |
|---|---|---|---|
| 1 | ROB-404 reconcile | `KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED`<br>`KIS_MOCK_RECONCILE_PERIODIC_ENABLED` | 체결 이벤트→즉시 reconcile(dry-run→실반영), 주기 reconcile |
| 2 | ROB-405A journal | `MOCK_ROUNDTRIP_JOURNAL_BRIDGE_ENABLED` | roundtrip→trade_journal 자동 마감 |
| 2 | ROB-405B verdict | `JOURNAL_VERDICT_AUTO_ENABLED` | closed journal→verdict 자동 기록 |
| 2 | ROB-405C counterfactual | `JOURNAL_COUNTERFACTUAL_ENABLED` | trigger/fill/no-action 기록(라이브 시세 read) |
| 2 | ROB-405E follow-up | `WATCH_FOLLOW_UP_LINK_ENABLED` | 회고→follow-up report item 환류 |
| **3 (마지막)** | **ROB-402 자동집행** | **`WATCH_AUTO_EXECUTE_MOCK_ENABLED`** | **auto_execute_mock watch→kis_mock 주문** (유일 broker-mutation 게이트) |

- **ROB-406**(cancel/modify)·**ROB-403**(zone/max_action 스키마)는 flag 없음 — 배포 즉시 활성. **ROB-405D**(`get_mock_loop_retrospective` MCP read)도 flag 없음(read-only).
- 2단계(405)는 read/record-only(broker mutation 없음)라 순서 무관·동시 가능.

## 3. TaskIQ 스케줄 등록 (현재 paused = recurring schedule 없음)
broker는 #1207로 5개 task를 **발견**하지만 schedule이 없어 자동 실행 안 함. operator가 cron을 추가(또는 수동 kick):

| task_name | 대응 |
|---|---|
| `kis_mock.reconcile_periodic` | 404 주기 reconcile |
| `mock_roundtrip.journal_sync` | 405A journal 마감 |
| `journal_verdict.sync` | 405B verdict |
| `journal_counterfactual.sync` | 405C counterfactual |
| `watch_follow_up.sync` | 405E follow-up 환류 |

- 이벤트구동 404 consumer는 task가 아니라 operator-run 프로세스: `uv run python -m scripts.kis_mock_execution_consumer run` (preflight 모드는 dry-run).
- 수동 1회 실행(스케줄 없이 검증용): 아래 CLI들 `run`/`sync` 모드.

## 4. Smoke (creds 필요, 코드와 별개 — evidence를 ROB-401/410에 첨부)
관측·기록 컴포넌트부터, 집행은 마지막:

1. **ROB-406 cancel/modify**: kis_mock 주문 후 `kis_mock_cancel_order`/`kis_mock_modify_order` 호출 → ledger orgno 직접 `VTTC0013U` 동작 확인. **broker가 미지원 시 fail-closed soft-cancel** 경고 + unsupported marker 기록(가짜 success 금지).
2. **ROB-404 reconcile**: 실 WS 체결 발행 → `scripts/kis_mock_execution_consumer.py run` → 해당 symbol reconcile 라운드트립 확인(중복 reconcile 없음).
3. **ROB-405 회고 파이프라인**(수동 sync):
   ```bash
   uv run python scripts/sync_mock_roundtrip_journals.py run      # A: journal
   uv run python scripts/sync_journal_verdicts.py sync            # B: verdict
   uv run python scripts/sync_journal_counterfactuals.py run      # C: counterfactual
   uv run python scripts/sync_watch_follow_up_items.py run        # E: follow-up
   ```
   → `get_mock_loop_retrospective` MCP(D)로 사이클 요약(armed/triggered/filled/PnL/hit-miss/verdict/CF) 확인.
4. **ROB-402 자동집행**(가장 마지막): `WATCH_AUTO_EXECUTE_MOCK_ENABLED=true` 후 `auto_execute_mock` watch 트리거 → kis_mock 주문 발생 + **live order 0 확인** + intent ledger(account_mode='kis_mock') 기록.
5. **end-to-end 1사이클**: arming(403) → 집행(402) → 체결(404 reconcile) → journal(405A) → verdict(405B) → counterfactual(405C) → follow-up 환류(405E) → `get_mock_loop_retrospective`(405D).

## 5. 롤백
각 flag를 `False`로 되돌리면 해당 컴포넌트 즉시 inert(다음 호출부터 no-op). 이미 기록된 mock 데이터(journal/verdict/...)는 mock 한정이라 무해; 필요 시 mock 행만 정리. TaskIQ는 schedule 제거 또는 task kick 중단.

## 6. Closure
- **코드 완성**: ROB-410 Wave 1+2+3 (406/403/404/402/405 A~E) + TaskIQ 등록(#1207) 전부 main.
- **ROB-401/410 closure** = 코드 완성(완료) + 위 활성화 + smoke evidence.

## 관련
- 오케스트레이션/PR 목록: ROB-410 (Linear) 종합 댓글.
- 컴포넌트별 상세: 각 PR(#1072/#1075/#1077/#1081/#1086/#1089/#1091/#1094/#1100/#1207) + spec/plan(`docs/superpowers/specs|plans/2026-06-0*-rob-40*`).
