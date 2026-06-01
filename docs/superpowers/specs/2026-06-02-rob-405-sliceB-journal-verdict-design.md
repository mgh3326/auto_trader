# ROB-405 Slice B — trade_journal verdict (good/neutral/bad)

- **이슈**: ROB-405 (G4) 회고 배선 — **Slice B (verdict)**
- **부모 에픽**: ROB-401, 트래커 ROB-410 Wave 3
- **선행**: Slice A (mock roundtrip→trade_journal, merged #1086 41d12c06)
- **작성일**: 2026-06-02
- **상태**: 설계 승인됨 → 구현 계획 수립 단계

## 1. 배경 / 현 상태

Slice A가 reconciled kis_mock roundtrip을 `trade_journal`(account_type='mock', correlation_id, entry/exit/pnl_pct)로 마감한다. Slice B는 그 closed journal에 **verdict(good/neutral/bad)** 를 pnl_pct 임계값으로 **자동** 기록하고 **수동 override(반자동)** 를 지원한다.

### 코드 매핑
- `app/models/trade_journal.py::TradeJournal`: status(draft|active|closed|stopped|expired), account_type(live|paper|**mock** — Slice A), pnl_pct(Numeric(8,4)), correlation_id(Slice A). closed mock journal이 verdict 대상.
- `app/models/review.py::TradeReview`(99–129): **`review.trades` FK**(journal 아님), verdict CHECK good|neutral|bad, review_type, comment, pnl_pct. → journal verdict은 **신규 테이블** 필요(기존 TradeReview 재사용 불가).
- Slice A 패턴: `app/services/trade_journal/`(브리지), config flag + paused taskiq + CLI, default-off.

## 2. 목표 / 비목표
**목표**: closed mock journal → verdict 자동 기록(pnl_pct 임계값) + 수동 override. 신규 `trade_journal_reviews` 테이블. default-off, 멱등, mock 전용 auto.
**비목표**: C counterfactual / D 사이클 read API(verdict 집계) / E follow_up. thesis/target-stop 보정. live journal auto-verdict(수동만).

## 3. 설계

### 3.1 데이터 모델 (신규 테이블 + 마이그레이션)
`review.trade_journal_reviews`:
- `id` BigInteger PK
- `journal_id` BigInteger FK→`review.trade_journals.id`(ondelete CASCADE), NOT NULL
- `verdict` Text NOT NULL — CHECK `verdict IN ('good','neutral','bad')`
- `verdict_source` Text NOT NULL — CHECK `verdict_source IN ('auto','manual')`
- `pnl_pct` Numeric(8,4) nullable (기록 시점 journal.pnl_pct)
- `comment` Text nullable
- `created_at` TIMESTAMP server_default now()
- Index `(journal_id)`. **Partial-unique** `(journal_id) WHERE verdict_source='auto'` (journal당 auto verdict 1개 → 멱등). manual은 복수 허용.
- 신규 테이블 → `Base.metadata.create_all`이 테스트 DB에 생성(conftest drift 패치 불요). alembic migration 추가.

### 3.2 자동 verdict 정책 (순수함수)
`app/services/trade_journal/journal_verdict_policy.py`:
```python
GOOD_PNL_PCT = Decimal("1.0")
BAD_PNL_PCT = Decimal("-1.0")

def classify_journal_verdict(pnl_pct: Decimal | None) -> str:
    if pnl_pct is None:
        return "neutral"
    if pnl_pct >= GOOD_PNL_PCT:
        return "good"
    if pnl_pct <= BAD_PNL_PCT:
        return "bad"
    return "neutral"
```
결정적. 임계값=모듈 상수(향후 config화 가능).

### 3.3 verdict 서비스 (`journal_verdict_service.py`)
- `sync_journal_verdicts(db, *, force=False) -> dict`: gate(`force or JOURNAL_VERDICT_AUTO_ENABLED`) → `status='closed'` + `account_type='mock'` + auto verdict 미존재 journal 스캔 → `classify_journal_verdict(journal.pnl_pct)` verdict insert(verdict_source='auto', pnl_pct, partial-unique로 멱등). 반환 `{status, created}`. **Slice A 브리지와 독립**(closed journal 스캔, A 무수정).
- `record_manual_verdict(db, *, journal_id, verdict, comment=None) -> dict`: verdict_source='manual' insert. verdict 값 검증(good|neutral|bad). 반자동 override(복수 허용).

### 3.4 발화 (default-off)
- `JOURNAL_VERDICT_AUTO_ENABLED: bool = False` (config). False면 auto sync `{"status":"disabled"}`.
- paused taskiq `journal_verdict.sync` + operator CLI(auto sync / manual record).

## 4. 컴포넌트
| 단위 | 위치 | 책임 |
|---|---|---|
| 모델/마이그레이션 | `app/models/review.py` + alembic | `trade_journal_reviews` 테이블 |
| 정책 | `app/services/trade_journal/journal_verdict_policy.py` | `classify_journal_verdict` 순수함수 |
| 서비스 | `app/services/trade_journal/journal_verdict_service.py` | `sync_journal_verdicts` + `record_manual_verdict` |
| config | `app/core/config.py` | `JOURNAL_VERDICT_AUTO_ENABLED=False` |
| task/CLI | `app/tasks/journal_verdict_tasks.py` + `scripts/sync_journal_verdicts.py` | paused + operator |

## 5. 안전 경계
- auto는 mock journal 전용(account_type='mock'). verdict 기록=DB-only(broker/order mutation 없음). default-off inert. 멱등(partial-unique). live journal verdict는 manual만.

## 6. 테스트
1. `classify_journal_verdict`: +1.5→good, -1.5→bad, 0.5→neutral, None→neutral, 경계값(±1.0 포함).
2. `sync_journal_verdicts`: closed mock journal(pnl_pct=+2)→auto 'good' row.
3. 멱등: 재실행 중복 auto verdict 없음(partial-unique).
4. non-closed / non-mock journal 무시.
5. `record_manual_verdict`: manual row(복수 허용), 잘못된 verdict 값 거부.
6. flag off → `disabled`, verdict 0.
7. 테이블 CHECK(verdict/verdict_source) + FK CASCADE.

## 7. 미해결 / 후속
- C counterfactual / D 사이클 read API(armed/triggered/filled/PnL/hit-miss, verdict 집계) / E follow_up_report_item_id.
- MCP verdict 도구(수동 override surface)는 D read API와 함께/별도.
- thesis/target-stop 보정, 임계값 config화, operator flag flip.
- 구현 시 origin/main(Slice A merged) 기준.
