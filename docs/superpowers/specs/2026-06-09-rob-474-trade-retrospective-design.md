# ROB-474 — 매매 회고(retrospective) 구조적 저장·집계 경로 (Design Spec)

- **Linear**: ROB-474 (Feature, Medium)
- **Branch / worktree**: `rob-474` / `/Users/mgh3326/work/auto_trader.rob-474`
- **작성일**: 2026-06-09
- **스코프**: 단일 PR (migration 1개 — `review.trade_retrospectives` 테이블만 신설)
- **관련**: ROB-405(회고 배선·정량 cycle 집계), ROB-459(item↔order=P4 deferred / typed evidence=P1), ROB-455(versioning/set_status), ROB-461(mock 손실매도), ROB-401(자율 루프), ROB-460(키움 read)

---

## 1. 배경 / 문제 (코드-grounded)

운영자 요청: "회고 분석 결과를 어딘가에 잘 저장." 현재 Claude는 매매 회고(근거·결과·교훈·다음전략)를 `investment_report_create`의 자유텍스트 `rationale`에 욱여넣는 수준이라 성과 시계열 집계·자동 피드백이 불가능하다.

**이슈 전제 검증 (8-agent 그라운딩, file:line):**

| 이슈 주장 | 판정 | 근거 |
|---|---|---|
| report item에 realized_pnl/fill_price/plan_price/outcome/lesson 없음 | ✅ TRUE | `app/models/investment_reports.py:298-330`. docstring `:206-207`이 **"execution state는 report item에 절대 두지 않음"** 불변식 명시 |
| 회고가 자유텍스트 rationale에 욱여넣어짐 | ✅ TRUE | `IngestReportItem.rationale` 필수 plain str(`app/schemas/investment_reports.py:265`). Slice E는 회고를 f-string으로 rationale에 박음(`watch_follow_up_service.py:111-116`) |
| **구조적 outcome 집계 없음 / 학습 누적 안 됨** | ⚠️ PARTIAL | 정량 집계는 **이미 존재**: `build_mock_loop_retrospective`(`mock_loop_retrospective_service.py:44-142`) + MCP `get_mock_loop_retrospective`가 armed/triggered/filled/avg_pnl_pct/hit_ratio/verdict/counterfactual을 KST-cycle별 집계. 단 ① default-OFF 4 플래그(`config.py:491-497`), ② watch-loop 한정, ③ **날짜 키(rationale 아님)**, ④ **정성 lesson/next_strategy typed 자리 없음**, ⑤ **절대 realized_pnl 없음(% 뿐)** |
| trade_journal이 mock 미지원 | ⚠️ PARTIAL | DB CHECK는 `account_type IN ('live','paper','mock')` **이미 허용**(`trade_journal.py:49-52`, migration `d8ed14023ef2`). bridge는 ORM으로 mock 저널 씀. 그러나 `save_trade_journal` 도구가 `live\|paper`만 받고 mock 거부(`trade_journal_tools.py:148`), `get_trade_journal` 기본값 `live`라 안 보임(`:248`) |
| 주문·저널에서 realized_pnl 자동 채움 (proposal A) | ❌ 막힘 | 절대 realized_pnl 컬럼이 **어디에도 없음**(`pnl_pct` % 뿐). reconcile는 PnL/체결가 booking 안 함(`kis_mock_reconciliation_job.py:139-167`은 observed_delta+attributed_fill_qty만). ledger gross/net_pnl은 scalping executor 전용→루프엔 NULL. item↔order 링크는 ROB-459 P4 **deferred** |
| 키움 회고는 주문접수까지만 | ✅ TRUE | 체결/PnL 파싱 경로 없음(`domestic_account.py:79-109`), 키움 mock은 매수만(ROB-460) |

**핵심 긴장점:** 이슈 proposal (A)의 "report item에 realized_pnl/fill_price 추가"는 모델 불변식과 정면충돌한다. 깔끔한 설계는 **체결 outcome은 journal-side에, 근거/교훈 서사는 그 row 안에 typed로** 두고 `correlation_id` 스파인(item→watch alert→event→ledger→journal에 이미 존재)으로 잇는 것.

---

## 2. Goals / Non-goals

**Goals**
1. **구조적 typed 회고 저장**: outcome / 절대 realized_pnl / fill_price / plan_price / pnl_pct / 근거·결과·교훈·다음전략을 1급 typed 컬럼으로 저장하는 `review.trade_retrospectives` 신설.
2. **집계 read path**: 근거(strategy_key)별·시간(KST window)별 win_rate / 절대 realized_pnl 합 / avg_pnl_pct 집계 (`get_retrospective_aggregate`). 이슈 제목의 "집계" 충족.
3. **mock 회고 작성 가능**: `save_trade_journal`에서 `account_type='mock'` 개방(도구 게이트만, migration 0).
4. **정직성**: 키움 mock은 fill 증거 없음 → 날조 금지(`fill_evidence_available=false`, realized_pnl 거부). 빈 집계 버킷 → `None`(0 아님).

**Non-goals (명시적 제외 — 후속 이슈)**
- ❌ item↔order 자동 fill (ROB-459 P4 의존). realized_pnl은 **caller 공급** 또는 journal 파생.
- ❌ reconcile의 realized_pnl/fill_price booking (별개 미해결 영역, 본 PR에서 안 건드림).
- ❌ **피드백 엣지**: report generator가 회고를 읽어 학습 / `follow_up_report_item_id` consumer 배선 — GAP, 후속. 본 PR은 "소비 가능한 집계 read 도구"까지.
- ❌ `strategy_key`를 `maybe_auto_execute`→ledger→journal로 **자동 스레딩**(자동 루프는 rationale을 버림, `watch_auto_execute.py:144`). v1 by-rationale 집계는 **caller 공급 strategy_key**로 동작.
- ❌ `investment_report_items` 스키마 변경 (불변식 준수). 회고는 `report_uuid`/`report_item_uuid`를 **참조만** 한다(ride-along 없음).
- ❌ `trade_journals` 테이블에 broker discriminator 컬럼 추가 (회고 row의 `account_mode`로 충분).
- ❌ HTTP 라우터 (v1은 MCP만). 필요 시 후속(ResearchRetrospective는 둘 다 보유).
- ❌ `TradeJournalWriteService`(ROB-120 operator 경로, `trade_journal_write_service.py:109/158`은 의도적 live-only) 변경. mock 개방은 **MCP `save_trade_journal` 한정**.

---

## 3. 데이터 모델 — `review.trade_retrospectives` (신규, migration 1개)

`TradeJournalCounterfactual`(`review.py:743-773`) / `TradeJournalReview`(`:703-740`) 패턴을 클론. `trade_journal_reviews`(auto-verdict 전용)와 **분리** — 회고는 작성 주체(Claude/operator)·라이프사이클이 다름.

### 3.1 컬럼 (review schema 컨벤션 준수)

| 컬럼 | 타입 | null | 비고 |
|---|---|---|---|
| `id` | BigInteger PK | — | surrogate PK (review 전 테이블 동일) |
| `correlation_id` | Text | ✓ | 라운드트립/주문 링크 **+ 멱등 키**. 값 있으면 upsert, NULL이면 append(다중 NULL 허용) |
| `journal_id` | BigInteger FK→`review.trade_journals.id` **ondelete=SET NULL** | ✓ | 회고는 journal보다 오래 사는 독립 기록 → CASCADE 아님(컨벤션 의도적 이탈, 문서화) |
| `report_uuid` | Text | ✓ | provenance: 회고가 다루는 investment_report |
| `report_item_uuid` | Text | ✓ | provenance: 특정 item (참조만, 스키마 변경 없음) |
| `symbol` | Text | ✗ | `to_db_symbol` 정규화(`shared.py:446`) |
| `instrument_type` | `Enum(InstrumentType, name="instrument_type", create_type=False)` | ✗ | enum 재사용(create_type=False 필수) |
| `side` | Text | ✓ | CHECK `side IS NULL OR side IN ('buy','sell')` |
| `account_mode` | Text | ✗ | CHECK IN (`kis_mock`,`kiwoom_mock`,`kis_live`,`alpaca_paper`,`upbit_live`) — GAP #6 해결 |
| `market` | Text | ✓ | kr/us/crypto (집계 필터) |
| `strategy_key` | Text | ✓ | **by-rationale 집계 group key** (caller 공급) |
| `outcome` | Text | ✗ | CHECK IN (`filled`,`partially_filled`,`unfilled`,`rejected`,`cancelled`) |
| `plan_price` | Numeric(20,4) | ✓ | 계획가 |
| `fill_price` | Numeric(20,4) | ✓ | 체결가 |
| `realized_pnl` | Numeric(20,4) | ✓ | **절대 손익** (신규 차원) |
| `realized_pnl_currency` | Text | ✓ | CHECK `IS NULL OR IN ('KRW','USD')` |
| `realized_pnl_source` | Text | ✓ | CHECK `IS NULL OR IN ('caller_supplied','derived_from_journal')` (정직 출처) |
| `pnl_pct` | Numeric(8,4) | ✓ | % (기존 호환) |
| `fill_evidence_available` | Boolean | ✗ | server_default `true`. kiwoom_mock → 강제 false |
| `rationale` | Text | ✓ | 근거 |
| `result_summary` | Text | ✓ | 결과 |
| `lesson` | Text | ✓ | 교훈 |
| `next_strategy` | Text | ✓ | 다음 전략 |
| `evidence_snapshot` | JSONB (`Mapped[dict\|None]`) | ✓ | 선택적 구조 컨텍스트 |
| `created_by_profile` | Text | ✓ | CLAUDE_ADVISOR/HERMES/operator |
| `created_at` | TIMESTAMP(tz) server_default `func.now()` | ✗ | |
| `updated_at` | TIMESTAMP(tz) server_default `func.now()` onupdate `func.now()` | ✗ | upsert로 갱신되므로 mutable 테이블 → updated_at 포함 |

### 3.2 제약 / 인덱스

- `UniqueConstraint("correlation_id", name="uq_trade_retrospectives_correlation_id")` — 멱등(Postgres는 다중 NULL 허용 → ad-hoc append + 링크 upsert 양립)
- CHECK: `ck_trade_retrospectives_account_mode` / `_outcome` / `_side` / `_currency` / `_pnl_source`
- Index: `ix_trade_retrospectives_correlation_id`, `_journal_id`, `_strategy_key`, `_symbol`, `_report_uuid`, 그리고 by-time 집계용 복합 `ix_trade_retrospectives_account_mode_created` (`account_mode`,`created_at`)

### 3.3 마이그레이션

- 파일: `alembic/versions/<rev>_rob474_trade_retrospectives.py`, **`down_revision = '20260609_rob455'`** (현재 head — impl 시 `uv run alembic heads`로 재확인, main 전진 시 rebase)
- `op.create_table(..., schema="review")` + `op.create_index(..., schema="review")`. server_default는 `sa.text("now()")`. downgrade는 index→table 순.
- **`trade_journals` 테이블은 건드리지 않음** (mock은 기존 CHECK가 이미 허용).

---

## 4. Write 표면 — `save_trade_retrospective`

회고를 rationale에 욱여넣던 통증을 직접 해소하는 1급 도구. broker mutation 없음 → `save_trade_journal`/`investment_report_create`와 동일하게 confirm/dry_run 불필요. 모든 쓰기는 `TradeRetrospectiveService`(+repository) 경유(CLAUDE.md: review 쓰기는 서비스 레이어만).

- **파일**: `app/mcp_server/tooling/trade_retrospective_tools.py`(도구) + `app/services/trade_journal/trade_retrospective_service.py`(서비스+repository)
- **서비스 검증/가드**:
  - `outcome`/`account_mode` enum 위반 → early-return `{"success": False, "error": ...}`
  - **키움 가드**: `account_mode='kiwoom_mock'` → `fill_evidence_available=false` 강제, `realized_pnl`/`fill_price` 제공 시 거부(날조 금지)
  - **realized_pnl 출처**: caller 공급 → `caller_supplied`. 미공급 + `journal_id` 있고 journal에 entry/exit/qty 있으면 `(exit−entry)×qty`(side 부호) 파생 → `derived_from_journal`. (주문 ledger 자동 fill은 ROB-459 P4 의존이라 제외)
  - `symbol`은 `to_db_symbol` 정규화
- **멱등**: `correlation_id` 있으면 `insert(...).on_conflict_do_update(constraint="uq_trade_retrospectives_correlation_id")`(`ExecutionLedgerRepository.upsert_fill` 클론), NULL이면 append.
- **에러 envelope**(전 trade_journal 도구 동일): 성공 `{"success": True, "action": "created"|"updated", "data": <serialize>}`(+선택 `"warning"`), 실패 `{"success": False, "error": "<msg>"}`, 최상위 `try/except` → `logger.exception("save_trade_retrospective failed")`.

---

## 5. Read / 집계 표면 — `get_trade_retrospectives` + `get_retrospective_aggregate`

`mock_loop_retro_registration.py`(read 도구) + `research_retrospective_service.build_stage_performance`(`:121-178`, win_rate/avg group-aggregate) 형태를 클론. 읽기 전용 module-level `async def build_*(db, *, ...)`.

**기존 `get_mock_loop_retrospective`와 비중복(non-overlap, 코드 확인):**
- 기존 = **KST day × watch-loop 한정 × 퍼센트**. watch-event correlation_id에 묶인 mock 저널만(`mock_loop_retrospective_service.py:76-77`).
- 신규 = **strategy_key × 자유 시간 window × 절대 realized_pnl**, **모든** mock 회고(수기 포함) 대상. 차원·소스셋·지표 모두 다름.

- **`get_retrospective_aggregate`**: params `kst_date_from/to`(기본 `now_kst().date()`), `market`, `account_mode`, `strategy_key`, `group_by ∈ {strategy, day}`(기본 strategy). per-group: `sample_size`, `win_rate_pct`(realized_pnl>0 또는 pnl_pct>0 / sample, sample=0이면 None), `avg_pnl_pct`(`_avg`), `realized_pnl_sum`(통화별 dict `{KRW:…, USD:…}`), `hits`/`misses`, `by_outcome`. **`fill_evidence_available=true`만 손익 집계**(제외 카운트는 별도 노출). 빈 버킷 → None.
- **`get_trade_retrospectives`**: 필터 `symbol`/`account_mode`/`strategy_key`/`market`/`correlation_id`/`days`/`limit`. 반환 `{"success": True, "entries": [...], "summary": {count, by_outcome}}`(list 도구는 `data` 아닌 `entries`/`summary`).

---

## 6. `trade_journal` mock 개방 (migration 0)

DB CHECK·bridge·`mock_loop_retrospective_service`는 이미 mock 사용 → **유일한 차단은 도구 검증 튜플**.

- `trade_journal_tools.py:148` — `("live","paper")` → `("live","paper","mock")`
- `:150` — 일관성: `account_type == "live"` → `account_type in ("live","mock")` (mock도 paper_trade_id 금지). `:155`의 `paper requires account`는 **mock에 확장하지 않음**(mock은 broker account 없음).
- `:248` — `get_trade_journal` 기본 `account_type="live"` → `None`(`:287` `if account_type is not None` 가드라 None=전체조회).
- 도큐/설명 갱신: docstring `:130`,`:257` + `trade_journal_registration.py:26-37,45-46`.
- **불변**: `trade_journal_write_service.py`(ROB-120 operator, live-only)는 변경 없음.

---

## 7. 교차 절단 제약 / 롤아웃

- **등록 4 touch point**: (1) 신규 `trade_retrospective_registration.py`에 `TRADE_RETROSPECTIVE_TOOL_NAMES: set[str]`(3개) + `register_trade_retrospective_tools` + `__all__`; (2) `registry.py` import + "Always: read-only with account_mode (mock-safe via ROB-28)" 블록(~`:123-130`)에서 호출(write도 broker mutation 없어 always 그룹); (3) `app/mcp_server/__init__.py:15-59` `AVAILABLE_TOOL_NAMES`에 3개 append; (4) (선택) `tooling/__init__.py` `_LAZY` 맵. ⚠️ ROB-376 교훈: `*_TOOL_NAMES` set 누락은 재발 버그.
- **모델 등록**: `TradeRetrospective`를 `app/models/review.py`에 추가(Base 발견용 import 체인 확인).
- **플래그 없음**: broker mutation·스케줄러 없는 inert 표면 → default-off 게이트 불필요(`investment_report_create`/`save_trade_journal`과 동일).
- **운영 cutover**: migration은 PR 포함하되 operator가 별도 `alembic upgrade head`(CLAUDE.md 게이트).
- **테스트(TDD)**: 서비스(enum 거부/키움 가드/realized_pnl 파생/멱등 upsert/ad-hoc append/serializer) · 집계(win_rate>0 기준/avg/통화별 sum/strategy·day group/evidence 필터/빈→None/div-0 가드) · MCP 계약(envelope success·action·data / entries·summary / error / TOOL_NAMES set 포함) · mock 개방(save mock 성공/get None이 mock 노출/mock은 account 불요/mock paper_trade_id 금지). ⚠️ 테스트는 alembic이 timescaledb ext로 막히므로 `db_session` create_all 픽스처로 테이블 생성(ROB-407 교훈) — impl 시 확인.
- **lint**: CI는 `app/` + `tests/` 둘 다 → 신규 테스트도 ruff clean(ROB-462/423 교훈, heredoc append 금지).

---

## 8. Deferred (별도 후속 이슈)

1. **피드백 엣지**: report generator/`JournalSnapshotCollector`가 회고·verdict를 읽어 다음 분석에 반영 + `follow_up_report_item_id` consumer 배선(GAP #3).
2. **strategy_key 자동 스레딩**: `maybe_auto_execute`→ledger→journal로 rationale/strategy 보존(GAP #4) → 자동 루프의 by-rationale 집계.
3. **realized_pnl 자동 fill**: ROB-459 P4(item↔order 링크) + reconcile PnL booking 완료 후 주문 ledger에서 자동 채움.
4. **키움 fill 회고**: ROB-460 해결 후 kiwoom_mock fill 증거 수집.
5. **HTTP 라우터** + **`trade_journals` broker discriminator 컬럼**(필요 시).

---

## 9. 핵심 코드 앵커 (grounding)

- 모델 클론: `app/models/review.py:703-740`(TradeJournalReview), `:743-773`(TradeJournalCounterfactual), `:179-263`(KISMockOrderLedger 타입/CHECK)
- report 불변식: `app/models/investment_reports.py:206-207,298-330`
- 정량 집계(비중복 근거): `app/services/trade_journal/mock_loop_retrospective_service.py:44-142,76-77`; MCP `app/mcp_server/tooling/mock_loop_retro_registration.py:16-55`
- 집계 클론: `app/services/research_retrospective_service.py:121-178,360-402`
- repository upsert: `app/services/execution_ledger/repository.py:67-134`
- write 도구 envelope/serialize/session: `app/mcp_server/tooling/trade_journal_tools.py:29-62,105-237`
- mock 개방 라인: `app/mcp_server/tooling/trade_journal_tools.py:148,150,155,248`; DB CHECK `app/models/trade_journal.py:49-52`
- 등록: `app/mcp_server/tooling/registry.py:54-56,123-130`; `app/mcp_server/__init__.py:15-59`; `app/mcp_server/tooling/trade_journal_registration.py:17-67`
- migration 클론: `alembic/versions/91097f38827e_rob405c_trade_journal_counterfactuals.py`; head `20260609_rob455`
- 계정 vocab: `app/schemas/investment_reports.py:40`(ACCOUNT_SCOPES); `app/mcp_server/tooling/account_modes.py:8-10`
