# ROB-693 — 리포트에 무효화 조건(invalidation_triggers) 구조화 필드 추가

Branch: `feature/ROB-693-invalidation-triggers`
Worktree: `/Users/mgh3326/work/auto_trader.rob-693` (based on main incl. ROB-690)

## 1. Goal

리포트 아이템에 "이 논지를 무효화할 조건"을 담는 additive 구조화 필드
`invalidation_triggers: list[str]` 를 추가한다. 성격은 서술적(narrative) advisory
텍스트다.

- **auto_trader 는 PERSIST + RENDER 만** 한다. 이 필드의 내용은 프로세스 밖의
  Hermes 가 저술하고 `hermes_ingest` 경유로 write-back 한다.
- **auto_trader 결정론 코드(deterministic generator/debate)는 이 필드를
  자기-생성(self-populate)하지 않는다** (ROB-501 in-process LLM 경계의 정신).
- 완전 additive, backward-compatible, **migration-0** (JSONB `evidence_snapshot`
  reserved key), 신규 enum/컬럼 없음.

## 2. Verified current state (file:line, 정정 포함)

검증은 실제 worktree(ROB-690 머지 후) 기준.

- **Numeric indicators 존재 — 확인됨.** `MarketSignals`
  (`app/schemas/research_pipeline.py:33`) 는 `rsi_14`/`atr_14`/`volume_ratio_20d`/
  `trend` 를 가진다. 지표 계산은 `app/analysis/indicators.py`. (task 의 "~33-41"
  정확.)
- **오늘 invalidation 필드 없음 — 부분 정정.** 가장 가까운 것은
  `SummaryOutput.warnings: list[str]` (`research_pipeline.py:120`) 인데 이는
  stale/UNAVAILABLE 플래그 전용이다. `SummaryOutput.detailed_text: str | None`
  (`research_pipeline.py:119`) 도 존재하나 결정론 빌더
  (`app/analysis/debate.py::_build_deterministic_v1`, L55–131) 가 이를 **설정하지
  않아 항상 None** 으로 남는다 — 확인됨.
  - **정정/주의: "invalidation" 이름이 이미 다른 의미로 쓰인다.**
    `WatchInvalidation` (`app/schemas/investment_reports.py:592`, `kind ∈
    {price_below, condition_text}`) 은 **watch 스캐너가 실행하는 구조화 트리거**
    로 `WatchRecommendationPayload.invalidation` (L654) 에 붙는다. ROB-693 의
    `invalidation_triggers` 는 이와 **다른 개념** — 스캐너를 구동하지 않는
    per-item 서술 advisory 이며 action/watch/risk 모든 아이템에 적용된다. 플랜/
    코드/리뷰에서 둘을 혼동하지 않도록 명시한다(네이밍 충돌 주의). `trigger_checklist`
    과도 구분: 후자는 watch 알림에 복사되는 실행 체크리스트다.
- **ROB-690 additive precedent — 확인됨 (핵심 참조).** 커밋 `1446e1e2`.
  - `IngestReportItem` 에 typed 입력 필드 `position_direction`
    (`investment_reports.py:335`) 추가.
  - reserved-key guard `_validate_reserved_evidence_snapshot_keys`
    (`investment_reports.py:368`) 에 `trade_setup` 항목 추가(L385–386).
  - ingestion write-time 에 `evidence_payload["trade_setup"] = ...`
    (`app/services/investment_reports/ingestion.py:585`) 로 **JSONB
    evidence_snapshot 에 병합 → migration-0**.
  - 단, **정정/구분**: `trade_setup` 은 **서버 계산(server-computed) reserved
    값**이라 caller 가 주입하면 "항상 거부"(L385: `if "trade_setup" in
    evidence_snapshot`). ROB-693 은 반대로 **Hermes/caller 저술 필드**이므로,
    미러링할 정확한 선례는 `trade_setup` 이 아니라 **ROB-459 `structured_evidence`
    / `entry_plan` 스타일**(typed 필드가 채워졌을 때만 evidence_snapshot 의 같은
    키를 거부하는 "중복 방지" 가드, L376–384) + ROB-690 의 write-time 병합
    메커니즘 조합이다.
- **Hermes ingest 경로 — 확인됨. 추가 배선 불필요.**
  `HermesCompositionResult.items` 는 `list[IngestReportItem]`
  (`app/schemas/hermes_composition.py:177`). `HermesCompositionIngestService`
  (`app/services/investment_stages/hermes_ingest.py:450`) 는
  `IngestReportRequest(... items=list(composition.items) ...)` (L464) 로 **필드별
  재매핑 없이 그대로** `InvestmentReportIngestionService.ingest` 에 전달한다.
  → **`IngestReportItem` 에 필드를 추가하면 Hermes 경로에 자동 배선**되고,
  `hermes_composition.py`/`hermes_ingest.py` 는 손대지 않는다. `trigger_checklist`
  도 정확히 같은 방식으로 흐른다(단, 그 필드는 전용 컬럼 보유 — 아래 참조).
- **Persistence 선례 — 확인됨.** `trigger_checklist` 은 **전용 DB 컬럼**을 가진다
  (`app/models/investment_reports.py:329,536`; ingestion L601 `insert_item(...,
  trigger_checklist=...)`). 반면 `structured_evidence`/`entry_plan`/`stop_loss`/
  `target_price`/`linked_order_ids`/`trade_setup` 은 **evidence_snapshot JSONB 에
  병합 → 컬럼 없음(migration-0)**. ROB-693 은 후자 패턴을 택한다(아래 §6 근거).
  read-back 은 `InvestmentReportItemResponse.evidence_snapshot: dict[str, Any]`
  (`investment_reports.py:801`) 가 JSONB 전체를 그대로 라운드트립하므로 **응답
  스키마 변경 불필요**.
- **Render — 확인됨.** `ItemRow`
  (`frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx`)
  은 `item.evidenceSnapshot` (Record<string, unknown>) 를 소비한다. ROB-690 이
  여기 R:R chip(L204–338, `parseTradeSetup`)을 막 추가함 — ROB-693 렌더는 **같은
  컴포넌트에 additive**. API 매핑 `evidenceSnapshot: asRecord(raw.evidence_snapshot)`
  (`frontend/invest/src/api/investmentReports.ts:186,258`) 이미 존재 →
  **frontend 타입/매핑 변경 불필요**, 렌더 컴포넌트만.

## 3. Design

### 3.1 Field shape — `invalidation_triggers: list[str]`

`IngestReportItem` 에 `invalidation_triggers: list[str] = Field(default_factory=list)`
추가.

**근거 (list[str] vs typed block):**
- `warnings` / `reasons` / `trigger_checklist` (모두 `list[str]`) 와 동형 —
  일관성.
- Hermes 저술 서술 bullet ("실적 가이던스 하향 시", "RSI 30 하회 지속 시" 등)에
  구조가 필요 없다. typed block `{metric, operator, threshold}` 는
  `WatchConditionPayload`/`WatchInvalidation`(스캐너 실행형)과 개념 중복이고,
  advisory-only 필드에 불필요한 검증 부담을 준다 → **reject**.
- 완전 additive, JSON-safe, 신규 enum 없음, 기본 `[]` 로 legacy shape 불변.
- (선택적 상한) `max_length=10` 정도의 방어적 상한을 둘 수 있음(`reasons` 선례).
  각 항목 길이 상한은 두지 않음(narrative). 상한은 리뷰에서 확정.

### 3.2 Persistence location — evidence_snapshot JSONB (migration-0)

ingestion write-time 에 값이 비어있지 않으면
`evidence_payload["invalidation_triggers"] = list(item_req.invalidation_triggers)`
로 병합. `structured_evidence`/`entry_plan` 패턴과 동일. **신규 컬럼 없음 →
migration 0.**

- read-back: `InvestmentReportItemResponse.evidence_snapshot` 가 그대로 노출 →
  프론트가 `item.evidenceSnapshot.invalidation_triggers` 로 수신.
- reserved-key guard(중복 방지): `_validate_reserved_evidence_snapshot_keys` 에
  ```python
  if self.invalidation_triggers and "invalidation_triggers" in self.evidence_snapshot:
      conflicts.append("invalidation_triggers")
  ```
  추가. (ROB-690 의 "항상 거부"가 아니라 ROB-459 의 "typed 필드가 채워졌을 때만
  중복 거부" 스타일 — caller 가 필드를 통해 값을 주는 게 정상 경로이기 때문.)

### 3.3 Hermes ingest wiring point

**추가 배선 없음.** `IngestReportItem` 에 필드가 생기면 `composition.items`
(list[IngestReportItem]) → `IngestReportRequest.items` → `ingestion.ingest` 경로로
자동 통과한다(`hermes_ingest.py:464`). `hermes_composition.py`/`hermes_ingest.py`
파일은 **손대지 않는다**. 이것이 ROB-690 이 세운 additive-IngestReportItem
계약의 핵심.

### 3.4 Boundary test (auto_trader 자기-생성 금지)

ROB-501 정신을 미러하는 정적 스캔 테스트를 추가한다: `app/**/*.py` 를 AST/텍스트
스캔하여 `invalidation_triggers` 심볼의 등장을 **allowlist** 로 제한한다.

- 허용: (a) 스키마 필드 정의(`app/schemas/investment_reports.py`),
  (b) ingestion pass-through(`app/services/investment_reports/ingestion.py`),
  (c) reserved-key guard 라인.
- 금지(테스트 실패 조건): `app/analysis/**`, `app/services/action_report/**`
  (결정론 generator) 등에서 `invalidation_triggers` 에 **비어있지 않은 값을
  할당**하는 코드. 즉 auto_trader 가 서술 내용을 합성하면 실패.

이는 "auto_trader persists + renders ONLY, Hermes authors" 경계를 코드로 고정한다.
(대안으로 결정론 generator 를 실행해 산출 아이템의 필드가 비어있음을 assert 하는
행위 테스트도 가능하나, generator 는 caller-supplied `request.items` 를 그대로
정규화하므로(`generator.py:_build_ingest_request`) 정적 스캔이 더 강한 불변식을
준다.)

## 4. Step-by-step

1. **Schema** (`app/schemas/investment_reports.py`)
   - `IngestReportItem` 에 `invalidation_triggers: list[str] =
     Field(default_factory=list)` 추가(`trigger_checklist` 인접, ~L338).
   - `_validate_reserved_evidence_snapshot_keys` (L368) 에 중복-거부 절 추가.
2. **Ingest** (`app/services/investment_reports/ingestion.py`)
   - evidence_payload 병합 블록(L536–585 인접)에
     `if item_req.invalidation_triggers:
      evidence_payload["invalidation_triggers"] = list(item_req.invalidation_triggers)`
     추가. `insert_item(...)` 인자/모델/DB 변경 없음.
3. **MCP tool 설명(선택, 문서만)** (`app/mcp_server/tooling/investment_reports_handlers.py`)
   - `CREATE_DESCRIPTION` 에 invalidation_triggers 는 advisory 서술 list[str] 이며
     evidence_snapshot reserved key 로 병합됨을 한 줄 명시(ROB-690 이 trade_setup
     설명 추가한 것과 동형). 코드 로직 변경 없음.
4. **Render** (`frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx`)
   - `parseInvalidationTriggers(evidenceSnapshot)` 헬퍼: `evidenceSnapshot
     .invalidation_triggers` 가 string[] 이고 비어있지 않을 때만 반환(런타임
     방어적 검증, `parseTradeSetup` 선례와 동형).
   - `ItemRow` 에서 R:R chip 근처에 "무효화 조건" 섹션(bullet list) 렌더. 값
     없으면 렌더 안 함(legacy shape 불변).
5. **Tests** (§5).

## 5. Test plan

- **Backend unit** (`tests/test_investment_report_item_evidence.py` 확장):
  - `invalidation_triggers` 기본값 `[]` (legacy 아이템 불변).
  - typed 필드 + `evidence_snapshot["invalidation_triggers"]` 동시 설정 시
    `ValidationError`(중복 reserved-key).
- **Backend ingestion write-through** (신규
  `tests/test_investment_report_invalidation_triggers.py`, ROB-690 의
  `tests/test_investment_report_risk_reward.py` 스타일):
  - 아이템에 `invalidation_triggers=["...","..."]` 로 ingest → 저장 후
    `evidence_snapshot["invalidation_triggers"]` 로 라운드트립(값/순서 보존).
  - 빈 리스트면 키 미생성(legacy JSONB shape 불변).
  - Hermes 경로: `HermesCompositionResult`(items 에 필드 포함) → 정상 통과
    (필드가 review-only invariant 를 깨지 않음).
- **Boundary(정적 스캔)** (신규
  `tests/services/action_report/snapshot_backed/test_no_self_authored_invalidation.py`
  또는 기존 no-internal-llm 테스트 인접): `app/**` 에서 allowlist 밖의
  `invalidation_triggers` 비어있지 않은 할당 부재를 assert.
- **Frontend** (신규
  `frontend/invest/src/__tests__/InvestmentReportBundleContent.invalidation.test.tsx`,
  `...tradeSetup.test.tsx` 스타일):
  - `evidenceSnapshot.invalidation_triggers = ["a","b"]` 인 아이템이 "무효화
    조건" 섹션과 각 bullet 을 렌더.
  - 필드 없음/빈 배열/비-string 요소 → 섹션 미렌더(방어적 파싱).
- 회귀: 기존 BundleContent 테스트(tradeSetup/proposal/linkedOrders 등) 및 ingestion
  스위트 그대로 통과(순수 additive).

## 6. Migration note

**Migration-0 (신규 alembic revision 없음).** 값은 기존 JSONB
`investment_report_items.evidence_snapshot` 에 `invalidation_triggers` 키로 병합되며
read-back 은 `InvestmentReportItemResponse.evidence_snapshot` 로 이미 노출된다.
`structured_evidence`/`entry_plan`/`stop_loss`/`target_price`/`linked_order_ids`/
`trade_setup`(ROB-690) 이 모두 동일하게 컬럼 없이 JSONB 에 사는 확립된 패턴과
일치한다.

**전용 컬럼을 쓰지 않는 이유:** `trigger_checklist` 은 watch 스캐너/알림
포매터(`app/jobs/investment_watch_scanner.py`, `formatters_*`)가 소비하므로 전용
컬럼 + `insert_item` 인자 + watch alert 전파가 필요했다. `invalidation_triggers` 는
**렌더 전용 advisory 서술**이라 인덱싱/스캐너 소비가 없어 JSONB 병합이 최소 표면·
최소 위험. 만약 후속에서 스캐너/집계가 이 필드를 쿼리해야 하면 그때 전용 컬럼
승격 마이그레이션을 별도 이슈로 낸다(현재는 불필요 — YAGNI).

## 7. Risks / out-of-scope

- **네이밍 충돌 위험**: 기존 `WatchInvalidation`(스캐너 실행형)과 혼동 금지. 리뷰
  노트/코드 주석에 "advisory narrative, 스캐너 미구동" 명시로 완화.
- **경계 회귀 위험**: 향후 누군가 결정론 generator 에서 이 필드를 합성하면 ROB-501
  정신 위반. §3.4 정적 스캔 테스트가 가드.
- **Out-of-scope**: (a) 스캐너/집계에서의 invalidation 소비, (b) SummaryOutput/
  ResearchSummary(연구 파이프라인 결정론 요약)로의 확산 — 그 표면은 결정론
  빌더가 채우므로 이 필드를 두면 경계 위반. 따라서 **IngestReportItem 에만** 둔다.
  (c) `WatchInvalidation` 로의 통합/리팩터. (d) Hermes 측 저술 로직(레포 밖).
- **최소 표면**: DB/모델/응답 스키마/Hermes 스키마 무변경. 신규 enum 무.

## Files this plan will touch

수정(edit):
- `app/schemas/investment_reports.py` — 필드 + reserved-key guard **[shared-file 주의]**
- `app/services/investment_reports/ingestion.py` — evidence_payload 병합
- `app/mcp_server/tooling/investment_reports_handlers.py` — CREATE_DESCRIPTION 한 줄(선택)
- `frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx`
  — 렌더 **[shared-file 주의]**

신규(new):
- `tests/test_investment_report_invalidation_triggers.py`
- `tests/services/action_report/snapshot_backed/test_no_self_authored_invalidation.py`
  (경계 정적 스캔)
- `frontend/invest/src/__tests__/InvestmentReportBundleContent.invalidation.test.tsx`

확장(append):
- `tests/test_investment_report_item_evidence.py` — 필드 accept/중복-거부 케이스

**손대지 않음(중요):** `app/schemas/hermes_composition.py`,
`app/services/investment_stages/hermes_ingest.py`, alembic/, DB 모델
(`app/models/investment_reports.py`), 응답 스키마 read-back — 모두 additive
IngestReportItem 계약 덕에 무변경.
