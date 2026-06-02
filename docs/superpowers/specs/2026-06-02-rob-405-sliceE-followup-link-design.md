# ROB-405 Slice E — follow_up_report_item_id 자동 채움 (루프 환류)

- **이슈**: ROB-405 (G4) 회고 배선 — **Slice E (마지막)**
- **부모 에픽**: ROB-401, 트래커 ROB-410 Wave 3
- **선행**: A(#1086)·B(#1089)·C(#1091) merged (D #1094 별개·무관)
- **작성일**: 2026-06-02
- **상태**: 설계 승인됨 → 구현 계획 수립 단계

## 1. 배경 / 현 상태

자율 루프의 마지막 고리: closed watch-구동 roundtrip + 회고 결과(verdict/pnl/counterfactual)를 **다음 사이클 입력**으로 환류해야 한다. `investment_watch_events.follow_up_report_item_id`(FK→investment_report_items, SET NULL, nullable)가 그 링크지만 **미사용·writer 없음**.

### 코드 매핑
- `InvestmentWatchEvent.follow_up_report_item_id`(app/models/investment_reports.py:693) FK→`review.investment_report_items.id`(SET NULL). writer 없음(확인됨).
- `InvestmentReportItem`: `report_id`(FK NOT NULL, CASCADE) → **item은 부모 report 필수**(standalone 불가). `item_kind`∈{action,watch,risk}, `operation`∈{...,review}, `intent`∈{...,trend_recovery_review}, `symbol`, `rationale`(NOT NULL), `evidence_snapshot` JSONB, `client_item_key`(idempotency 구성). watch-invariant CHECK는 `operation IN ('cancel','keep','review')`를 watch_condition/valid_until 요구에서 **면제** → review-op item은 condition 없이 valid. **신규 enum/migration 불요**.
- ingestion: `InvestmentReportIngestionService.ingest_with_outcome(IngestReportRequest)`(ingestion.py:139) — atomic bundle, `report_key`(report_type+market+market_session+kst_date+generator_version) 멱등. `IngestReportRequest`: report_type(free str, CHECK 없음)/market(kr|us|crypto)/account_scope/execution_mode/created_by_profile/title/summary/kst_date/items. item 생성은 이 경로뿐(standalone insert_item은 저수준).
- repo: `insert_report`/`list_items_for_report(report_id)`/`update_event_delivery`(있음). **`update_event_follow_up` 없음 → 신규 writer 필요**.
- 링크 키: event.correlation_id == journal.correlation_id; verdict=trade_journal_reviews(journal_id); CF=trade_journal_counterfactuals(correlation_id).

## 2. 목표 / 비목표
**목표**: closed+verdict watch event(FK null)에 대해 회고 review item을 thin follow-up 리포트에 만들고 `follow_up_report_item_id` 설정. ingestion 서비스 재사용(직접 SQL 없음), default-off, 멱등, mock 한정. **마이그레이션 불요**.
**비목표**: 다음-사이클 리포트가 이 item을 소비하는 생성측 로직. HTTP 표면. operator flip. armed/D와 무관.

## 3. 설계

### 3.1 서비스 (`app/services/trade_journal/watch_follow_up_service.py`)
`sync_watch_follow_up_items(db, *, force=False) -> dict`:
- gate(`force or WATCH_FOLLOW_UP_LINK_ENABLED`) → 미통과 `{"status":"disabled","linked":0}`.
- **eligible events**: `InvestmentWatchEvent` where `follow_up_report_item_id IS NULL` AND correlation_id가 `trade_journals`(account_type='mock', status='closed') + 그 journal의 `trade_journal_reviews`(verdict) 보유. (join: event→journal(correlation_id)→review(journal_id))
- `(kst_date, market)`로 그룹핑.
- 각 그룹 → `IngestReportRequest` 1개:
  - `report_type='mock_loop_followup'`, `market`, `account_scope='kis_mock'`, `execution_mode='mock_preview'`, `created_by_profile='rob405_followup'`, `title/summary`(synth), `kst_date`=그 날, `status='draft'`.
  - `items`=eligible event당 1개: `item_kind='watch'`, `operation='review'`, `intent='trend_recovery_review'`, `symbol`=event.symbol, `target_kind='asset'`, `client_item_key=correlation_id`, `rationale`=`verdict`+`pnl_pct`+CF deltas 합성, `evidence_snapshot={"correlation_id":cid,"verdict":...,"pnl_pct":...,"fill_vs_trigger_pct":...,"no_action_vs_fill_pct":...}`.
  - `ingest_with_outcome(...)` → 멱등(report_key). 
- ingest 후 `list_items_for_report(report.id)` → `evidence_snapshot["correlation_id"]`로 cid→item.id 매핑 → 각 eligible event에 `repository.update_event_follow_up(event.id, follow_up_report_item_id=item.id)`.
- **멱등**: FK 이미 set인 event 제외(eligible 필터) + report_key 재사용. 반환 `{status, linked}`.
- commit at end(B/C 패턴).

### 3.2 repository 신규 writer
`update_event_follow_up(event_id, *, follow_up_report_item_id)` — `update_event_delivery` 미러(sa.update set).

### 3.3 발화 (default-off)
`WATCH_FOLLOW_UP_LINK_ENABLED: bool = False` + paused taskiq `watch_follow_up.sync` + operator CLI(force run).

## 4. 컴포넌트
| 단위 | 위치 | 책임 |
|---|---|---|
| 서비스 | `app/services/trade_journal/watch_follow_up_service.py` | `sync_watch_follow_up_items` |
| repo writer | `app/services/investment_reports/repository.py` | `update_event_follow_up` |
| config | `app/core/config.py` | `WATCH_FOLLOW_UP_LINK_ENABLED=False` |
| task/CLI | `app/tasks/watch_follow_up_tasks.py` + `scripts/sync_watch_follow_up_items.py` | paused + operator |

## 5. 안전 경계
mock 한정(account_scope='kis_mock', mock journal+verdict event만). report/item 쓰기는 **기존 ingestion 서비스 경유**(atomic+idempotency 재사용, 직접 SQL 없음). event FK 갱신만 신규 writer. broker/order mutation 없음. default-off inert. 멱등(FK null + report_key). 마이그레이션 0. A/B/C/D 무변경.

## 6. 테스트
1. eligible(closed mock+verdict, FK null) event → follow-up 리포트+review item 생성 + event.follow_up_report_item_id == 그 item.id(evidence_snapshot.correlation_id 매핑).
2. verdict 없는 closed journal event → skip(FK null 유지, item 없음).
3. FK 이미 set인 event → skip.
4. 멱등: 재실행 → 추가 item/리포트 생성 없음, 중복 link 없음.
5. (kst_date, market) 그룹 분리(다른 날/market → 별 리포트).
6. flag off → `disabled`, link 0.
7. repo `update_event_follow_up` 단위(event.follow_up_report_item_id 설정).

## 7. 미해결 / 후속
- 다음-사이클 리포트가 follow-up item을 실제 소비하는 생성측 로직(별개). HTTP 표면. operator flag flip + smoke.
- **ROB-405 closure**: A~E 코드 완성; 전체 default-off라 closure=코드완성+operator 활성화.
- 구현 시 origin/main(A/B/C merged) 기준.
