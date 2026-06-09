# ROB-345 — US kis_live 리포트 trade journal 매핑 복구 (Design)

- **Issue**: ROB-345 (Bug / High) — parent ROB-336 (`/invest/reports` Phase 2)
- **Date**: 2026-06-09
- **Scope**: PR-A (A→B→C 배치). Migration-0. 독립(346/347과 무충돌).
- **Status**: design + adversarial spec-review 반영. pending user review → plan

## 1. Context

US snapshot-backed `/invest/reports` readiness 점검에서, 직접 조회한 live US trade
journal에는 active 항목이 있었으나 생성된 snapshot/Hermes context의 journal stage는
`open journal: none` 으로 표시되었다. 원 이슈는 원인을 `account_scope="kis_live"` →
live KIS journal 변환 누락으로 추정.

## 2. Root cause — 이슈 가설 정정 (코드 검증, DB 불요)

검증 결과 제출 가설(account_scope 변환 누락)은 *부차적* latent 결함이고, "open journal:
none" 의 *지배적 proximate 원인*은 journal snapshot **payload 키 계약 불일치**다.

### 2.1 (지배 원인) journal payload 키 계약 불일치
- Collector(`collectors/journal.py:66-67`)는 payload 키를 **`active` /
  `recent_retrospective`** 로 emit.
- Stage(`stages/portfolio_journal.py:118`)는 **`(snap.payload_json or {}).get("entries", [])`**
  를 읽음. **`entries` 키는 어떤 collector도 쓰지 않는다.**
- 따라서 stage가 `.get("entries")` 를 호출하는 한, payload에 active journal이 몇 개
  있든 **entries는 항상 `[]`** → `symbols == ""` → `open journal: none` 이
  **모든 market(KR/US)에서 항상** 출력. (journal 내용과 무관한 키 미스매치.)
- 게다가 `missing_data = [] if journal_snaps else ["journal"]`
  (`portfolio_journal.py:137`): journal snapshot은 *존재*하므로 missing으로도 표시되지
  않아 **조용히 none** → 증상과 정확히 일치.
- `portfolio_journal.py:108` 이 `snapshots_for("journal")` 의 **유일한 소비자**
  (app/ 내 grep 확인).
- 단위 테스트(`tests/services/investment_stages/stages/test_portfolio_journal.py:40,287`)가
  `{"entries": [...]}` 를 **fabricate** 해 stage의 잘못된 기대와 맞춰 통과 → 버그를
  가린 test-gap.

### 2.2 (부차) market 미스코핑 + provenance 부재
- Collector는 `account_type=="live"` + status 로만 필터(`journal.py:44-60`).
  `market`/`instrument_type`/`account` 필터 없음 → US 리포트가 KR live journal까지 섞어
  가져온다(2.1 키를 고치면 즉시 노출될 mixing; ROB-336 authority separation 위반).
- `_journal_to_dict`(`journal.py:89-111`)는 `instrument_type`/`account_type`은 emit하나
  **`account` 누락** → provenance 검증 불가.

### 2.3 empty vs unavailable 미구분
"active journal 없음(정상 empty)" 과 "collector가 못 돈 경우(unavailable)" 미구분.

### 2.4 schema / 작동하는 참조 구현
- `app/models/trade_journal.py`: `instrument_type` enum(`equity_kr`/`equity_us`/`crypto`/
  `forex`/`index`), `account`(unconstrained Text), `account_type` CHECK(`live`/`paper`/`mock`).
  **`market`/`broker` 컬럼 없음** → no-migration discriminator는 `instrument_type`뿐.
- KIS-live ledger 체결 기록(`app/mcp_server/tooling/kis_live_ledger.py:504,511,535,536,549,550`)은
  journal을 `account_type="live"`, `account="kis"`, `instrument_type=<equity_us|equity_kr>` 로 씀.
- **작동하는 참조: `get_trade_journal`** (`app/mcp_server/tooling/trade_journal_tools.py:240-304`)
  은 이미 `account_type` + `account` + `market`(→ `market_map` → `instrument_type`,
  `:296-304`)로 필터한다. AC가 "get_trade_journal(market='us')에서 active US가 보인다"
  라고 한 이유. **본 PR은 리포트 collector를 이 참조 동작에 맞추는 것.**

## 3. Goal

US kis_live 리포트 journal stage가 active US KIS journal을 정확히 반영하고, KR/타 market
journal과 섞이지 않으며, "journal 없음" 과 "collector unavailable" 을 구분된 reason으로
남긴다. **DB backfill/쓰기 없이, migration-0.** 리포트 collector를 `get_trade_journal`
참조 필터와 일치시킨다.

## 4. Design (read-only / additive, 3 레이어)

### 4.1 Layer 1 — payload 계약 정렬 (지배 수정)
- `portfolio_journal.py` stage가 collector 실제 키 **`active`** (open journals)를 읽도록
  수정. summary semantic = active journal symbols 최대 5개(기존 포맷 유지).
- `recent_retrospective` 은 본 PR summary 범위 밖. **현재 어떤 stage/service도 journal
  snapshot의 `recent_retrospective` 를 소비하지 않음**(grep 확인; delta/retrospective는
  별도 service 경로). 수집은 되나 stage 표면화는 follow-up.
- **테스트 fixture 정정**: `test_portfolio_journal.py:40,287` 을 collector 실제 shape
  (`{"active":[...], "recent_retrospective":[...], "active_count":n, "collector_status":"ok"}`)로
  변경 → 계약을 테스트로 고정(회귀 방지). 이게 없으면 2.1 버그가 다시 통과한다.

### 4.2 Layer 2 — market scoping + provenance (참조 미러)
- `collectors/journal.py` collect()가 **`get_trade_journal` 의 검증된 필터 패턴**
  (`trade_journal_tools.py:296-304` 의 `market_map`)을 미러:
  - `TradeJournal.account_type == "live"` (유지)
  - `TradeJournal.instrument_type == market_map[request.market]`
    (us→`equity_us`, kr→`equity_kr`; 매핑은 `market_map` 과 동일 값으로 정의.
    shared 헬퍼 없으면 collector 내부에 명시 정의 + 출처 주석. symbol collector
    `collectors/symbol.py:229` 도 `equity_us` 하드코딩이라 helper 부재 확인됨.)
  - `(TradeJournal.account == "kis") | (TradeJournal.account.is_(None))`
- `_journal_to_dict` 에 **`account` 추가**(provenance).
- **legacy NULL allowance**: 현재 write는 `account="kis"`(kis_live_ledger 확인). 과거 일부
  live row가 `account=NULL` 일 수 있어 `account IS NULL` 을 허용(포착은 하되 강제 안 함)
  — migration-0 하에 historical 데이터 보존. crypto 등 타 instrument_type은 자동 제외.
- **읽기 서비스(`trade_journal_read_service.list_retrospective`)는 본 PR 범위 아님**:
  리포트 collector는 자체 쿼리를 쓰고, `get_trade_journal` 은 이미 정확. retrospective
  read service 수정은 불필요 scope creep → 제외.

### 4.3 Layer 3 — empty vs unavailable 구분
- Collector: 정상 시 `build_result` 로 `active`/`recent_retrospective`/counts +
  **`collector_status: "ok"`** 를 payload에 emit. SELECT 예외 시
  `unavailable_result(reason="journal_query_failed")` 반환.
  - unavailable 트리거(현 PR): SELECT 쿼리 예외 → `journal_query_failed`. (timeout/
    freshness 기반 unavailable은 follow-up.)
- Stage 판정:
  - journal snap 없음 **또는** snap이 unavailable(`freshness_status=="unavailable"`
    또는 payload `collector_status != "ok"`) → `missing_data += ["journal"]`, data_gap
    reason `journal_collector_unavailable`.
  - snap 정상 + `active == []` → "open journal: none" 유지 + reason `no_open_journals`
    (정상 상태, confidence 미감점).
  - snap 정상 + `active` 존재 → symbols 렌더.
  - (StageContext snapshot의 freshness 속성 접근은 plan에서 확정. 1순위는 payload
    `collector_status` — 속성 의존 최소화.)

## 5. Acceptance criteria (이슈 매핑)
- `get_trade_journal(account_type="live", market="us")` 의 active US KIS journal이
  snapshot-backed US report journal stage summary에 반영. → L1+L2
- `account_scope="kis_live"`(→ market scoping) regression test 존재. → L2
- journal 없음(`no_open_journals`) 과 collector unavailable(`journal_collector_unavailable`)
  구분되어 data_gap에 남음. → L3
- KIS live와 KR/Toss/reference/manual authority 미혼합(US 리포트에 KR journal 미포함). → L2

## 6. Test plan
1. **계약 회귀(지배)**: collector 실제 shape(`active`)로 stage가 symbol 렌더 —
   `entries` fabrication 제거 후에도 그린.
2. **market scoping (DB-seeded integration)**: `trade_journals` 에 live equity_us
   (account="kis") + live equity_kr(account="kis") row를 seed. collect(market="us")는
   equity_us만, collect(market="kr")는 equity_kr만 반환. account="kis"/NULL 포함, 타
   account 제외 → ROB-336 per-market authority separation 증명.
3. **provenance**: `_journal_to_dict` 가 `account` emit.
4. **empty vs unavailable**: (a) active 0개 → `no_open_journals`, confidence 미감점;
   (b) collector unavailable(쿼리 예외 mock) → `journal_collector_unavailable` data_gap.

## 7. Safety boundaries / Non-goals
- journal row 대량 수정/backfill **금지**(collector read-only; `TradeJournalWriteService`
  유일 write, import 안 함). broker/order/watch/order-intent mutation·주문
  preview/submit/cancel/modify 금지.
- Toss/manual holdings를 KIS live journal로 승격 금지.
- KR 경로 회귀 금지(scoping은 market별 additive; KRW summary byte-identical 계약 유지).
- **read consistency**: collector는 기본 AsyncSession 격리(READ COMMITTED). collect 중
  concurrent fill로 count가 payload list와 미세 불일치 가능 — 기존과 동일 fail-open 허용
  (엄격 격리는 follow-up).
- dead `app/services/action_report/us/` 모듈(zero non-test caller; action_classifier의
  journal 읽기 포함)은 **본 PR에서 미수정/미제거** — 별도 cleanup 이슈.
- migration 없음.

## 8. Out of scope / follow-up
- 실패 리포트 실제 DB row 대조(원인은 코드로 규명; 필요 시 operator).
- `trade_journals` 명시 `market`/`broker` 컬럼(additive migration) — cross-broker live가
  `account_type="live"` 공유 시 검토.
- `recent_retrospective` stage 표면화; dead `us/` 제거.
