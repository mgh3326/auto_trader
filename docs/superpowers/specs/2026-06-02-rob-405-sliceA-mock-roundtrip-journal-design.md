# ROB-405 Slice A — mock roundtrip → trade_journal 자동 마감

- **이슈**: ROB-405 (G4) 회고 배선 — **Slice A (척추)**
- **부모 에픽**: ROB-401 (모의 자율매매 루프), 오케스트레이션 트래커 ROB-410 Wave 3
- **선행**: ROB-402 (correlation_id watch→order, merged), ROB-404 (reconcile, merged)
- **작성일**: 2026-06-02
- **상태**: 설계 승인됨 → 구현 계획 수립 단계

## 0. 슬라이싱

ROB-405는 5 deliverable 에픽급 → 슬라이스로 분할. **Slice A = 척추**(mock roundtrip→journal). 후속: B verdict / C counterfactual / D 사이클 read API / E follow_up_report_item_id. 각 별도 spec/plan.

## 1. 배경 / 현 상태

자율 루프 결과를 LLM이 정량 회고하려면 watch→집행→체결 체인이 `trade_journal`(entry/exit/pnl)로 마감돼야 한다. 그러나:

- **mock 주문은 trade_journal을 만들지 않는다**: `order_journal.py`의 `_create_trade_journal_for_buy`/`_close_journals_on_sell`는 live/order_execution 경로에서만 호출. mock `_place_order_impl(is_mock=True)`→`_record_kis_mock_order`는 KISMockOrderLedger만 쓰고 live journal/fill 경로를 건너뜀.
- **roundtrip 페어링은 미자동화**: ROB-321이 `KISMockOrderLedger.correlation_id`/`scalping_role`('entry'|'exit')/`exit_reason`/`gross_pnl`/`net_pnl` 컬럼만 추가. 페어링 서비스 없음.
- **ROB-402**가 watch→order 시 `KISMockOrderLedger.correlation_id`(+InvestmentWatchEvent.correlation_id) 설정.
- **ROB-404**가 reconcile로 `lifecycle_state='fill'/'reconciled'` 마킹.

### 코드 매핑
- `app/models/trade_journal.py::TradeJournal`(38–): entry_price/quantity/amount, exit_price/exit_date/exit_reason/pnl_pct(Numeric(8,4)), thesis/strategy, status(default 'draft', CHECK draft|active|closed|stopped|expired), side(buy|sell), account/account_type(default 'live', CHECK **live|paper**), extra_metadata JSONB. **correlation_id 컬럼 없음**.
- `app/models/review.py::KISMockOrderLedger`: correlation_id/scalping_role/exit_reason/gross_pnl/net_pnl, lifecycle_state(…fill|reconciled…), symbol/side/quantity/price/amount, thesis/strategy/reason.
- close pnl 패턴 참고: `app/mcp_server/tooling/order_journal.py::_close_journals_on_sell`(FIFO, exit_price/exit_date/exit_reason/pnl_pct) — live 경로, 무변경.

## 2. 목표 / 비목표

**목표**
- reconciled mock roundtrip(correlation_id 페어링)으로 `trade_journal` 자동 entry(active)/exit(closed)/pnl 마감.
- watch→order→journal 감사 링크(correlation_id).
- default-off inert, 멱등, mock 전용(live journal 경로 무변경).

**비목표 (후속 슬라이스)**
- verdict(B) / counterfactual(C) / 사이클 read API(D) / follow_up_report_item_id(E).
- operator flip + live-mock smoke.
- correlation_id 없는 수동 mock 주문 journaling.

## 3. 설계

### 3.1 데이터 모델 (additive)
- `trade_journals.correlation_id` Text nullable + `ix_trade_journals_correlation_id` — watch→order→journal 링크 + 브리지 멱등 키.
- `trade_journals_account_type` CHECK 교체: `account_type IN ('live','paper','mock')`.
- migration + conftest×(필요 시) DDL drift 패치(컬럼 add-if-not-exists + CHECK drop/recreate).

### 3.2 브리지 서비스 — `app/services/trade_journal/mock_roundtrip_journal_bridge.py`
`async def sync_mock_roundtrip_journals(db) -> dict`:
1. `correlation_id IS NOT NULL` AND `lifecycle_state IN ('fill','reconciled')` AND `account_mode='kis_mock'`인 KISMockOrderLedger 행 조회, correlation_id로 그룹핑.
2. correlation_id별:
   - **entry**(side='buy' 또는 scalping_role='entry') 존재 & 해당 correlation_id의 trade_journal 없음 → active journal 생성: `entry_price=ledger.price`, `quantity`, `amount`, `thesis=ledger.thesis or "auto: kis_mock roundtrip"`, `strategy`, `side='buy'`, `account_type='mock'`, `account='kis_mock'`, `instrument_type`, `correlation_id`, `status='active'`.
   - **exit**(side='sell' 또는 scalping_role='exit') reconciled & journal `status='active'` → 마감: `exit_price=ledger.price`, `exit_date=now`, `exit_reason=ledger.exit_reason or "roundtrip_exit"`, `pnl_pct = ((exit_price-entry_price)/entry_price)*100` (entry>0; else None), `status='closed'`. ledger.net_pnl/gross_pnl는 `extra_metadata`에 보존.
3. **멱등**: correlation_id로 journal 조회 → 이미 active면 재생성 안 함, 이미 closed면 재마감 안 함. 반환 dict: created/closed counts + correlation_ids.
4. **mock 전용 writer**: 이 서비스만 `account_type='mock'` journal을 write(ORM 직접; live write-service의 exit-field 가드와 무관). live journal(account_type live|paper) 절대 안 건드림.

### 3.3 발화 (default-off)
- `MOCK_ROUNDTRIP_JOURNAL_BRIDGE_ENABLED: bool = False` (config). False면 브리지 호출이 `{"status":"disabled"}` 반환(또는 호출 자체 skip).
- **paused taskiq task** `app/tasks/mock_roundtrip_journal_tasks.py::mock_roundtrip_journal_sync`(schedule 없음) + operator CLI `scripts/sync_mock_roundtrip_journals.py`. 404 reconcile와 느슨결합(자동 스케줄 없음; operator/flip로만).

## 4. 컴포넌트 / 인터페이스
| 단위 | 위치 | 책임 |
|---|---|---|
| 모델/마이그레이션 | `app/models/trade_journal.py` + alembic + conftest | correlation_id 컬럼 + account_type 'mock' |
| 브리지 | `app/services/trade_journal/mock_roundtrip_journal_bridge.py` | reconciled ledger→journal entry/close, 멱등, mock 전용 |
| config | `app/core/config.py` | `MOCK_ROUNDTRIP_JOURNAL_BRIDGE_ENABLED=False` |
| taskiq task | `app/tasks/mock_roundtrip_journal_tasks.py` | paused, env-gated |
| CLI | `scripts/sync_mock_roundtrip_journals.py` | operator preflight/run |

## 5. 안전 경계
- **mock 전용**: account_type='mock' + kis_mock ledger만. live journal(live|paper) 경로 코드 무변경.
- broker/order mutation 없음(ledger read + journal write만).
- **default-off inert**: flag False면 journal 생성 0. 스케줄러 auto-start 없음.
- 멱등(correlation_id). 마이그레이션 additive(컬럼+CHECK).

## 6. 테스트
1. entry reconciled → active journal(account_type='mock', correlation_id, entry_price/quantity/thesis).
2. entry+exit reconciled → journal closed + exit_price + pnl_pct(=((exit-entry)/entry)*100) + exit_reason.
3. 멱등: 두 번 실행 → 중복 journal/재마감 없음.
4. correlation_id 없는 ledger 행 무시.
5. live journal(account_type live|paper) 행은 브리지가 안 건드림(회귀).
6. flag False → `disabled`/no-op, journal 0.
7. 모델/마이그레이션: account_type 'mock' 허용 + correlation_id 컬럼 존재.

## 7. 미해결 / 후속
- Slice B verdict / C counterfactual / D 사이클 read API / E follow_up_report_item_id.
- operator flip(`MOCK_ROUNDTRIP_JOURNAL_BRIDGE_ENABLED`) + live-mock smoke.
- 404 reconcile 완료 후 자동 호출 배선(현재 느슨결합; 후속에서 검토).
- 구현 시 origin/main(402 merged) 기준 rebase.
