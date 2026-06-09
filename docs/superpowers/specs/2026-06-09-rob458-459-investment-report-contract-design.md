# ROB-458 / ROB-459 — investment_report_create 계약 노출 + advisory 근거·체이닝 (Design Spec)

- **Issues:** [ROB-458](https://linear.app/mgh3326/issue/ROB-458) (Bug), [ROB-459](https://linear.app/mgh3326/issue/ROB-459) (Feature)
- **Date:** 2026-06-09
- **Status:** Approved design → writing-plans
- **Scope this round:** Slice 1 (ROB-458 전체) → Slice 2 (ROB-459 P3) → Slice 3 (ROB-459 P1). **Deferred:** ROB-459 P2 (graded lite reports / generator gate), P4 (report_item↔order linkage) — 별도 후속 이슈.

---

## 1. 배경 / 문제 (코드-grounded)

`investment_report_create` MCP 도구로 advisory 리포트를 올릴 때 두 부류의 마찰이 있다.

**A. 계약 미노출 + 순차 거부 (ROB-458 — Bug).**
- 핸들러 시그니처가 `items: list[dict[str, Any]] | None = None` (`app/mcp_server/tooling/investment_reports_handlers.py:248`)이라 FastMCP가 `items[]` 요소를 `additionalProperties: true`(형태 없음)로 노출한다.
- 그런데 런타임은 `[IngestReportItem.model_validate(it) for it in (items or [])]` (`handlers.py:283`)로 **숨은 필수 필드/enum**을 강제한다. 이 list comprehension은 **첫 번째 잘못된 item에서 즉시 `ValidationError`를 던지고** 끝나므로, 호출자는 필드명 누락 → 값 오류를 **순차적으로** 발견한다(2026-06-09 라이브 3-round 재현).
- 등록부 `description=`(`handlers.py:851-856`)은 계약을 **전혀** 기술하지 않는다("Persist one ROB-265 investment_report bundle …").
- **이미 옆 도구에 정답 패턴이 있다.** `investment_report_generate_from_bundle_impl`(`handlers.py:715-759`)은 item별로 검증해 **모든 위반을 한 번에** 모으고 `required_fields` + `enums` + `notes` 블록까지 반환한다. `create`만 이걸 안 쓴다.

**A-1. 전제 정정 (중요).** ROB-458 본문은 "CLAUDE.md 운영노트가 `target_kind∈{asset,index,fx}`라고 **틀리게** 적었다"고 주장하나, **이 레포의 어떤 `.md`에도 그런 잘못된 운영노트는 없다.** `target_kind`는 옛 설계 플랜(ROB-265 / ROB-16)에서만 등장하며 거기서는 **정확하게** 쓰였다(watch 스캐너의 asset/index/fx dispatch 필드). 실제 두 필드는 별개다:
  - `item_kind ∈ {action, watch, risk}` — **필수**, 항목 분류.
  - `target_kind ∈ {asset, index, fx}` — **선택**, 기본값 `"asset"`, 악기 클래스(watch 스캐너 dispatch).
  따라서 ROB-458 제안 #3은 "틀린 노트 수정"이 아니라 **"없는 계약 노트를 신규 추가"**로 재정의한다.

**B. advisory 리포트의 근거·체이닝 부재 (ROB-459 — Feature).**
- ✅ `market_snapshot`/`portfolio_snapshot`은 호출자가 넘긴 dict가 그대로 저장된다(긍정).
- ❌ 항목별 근거(컨센/수급/종토 등)가 자유 텍스트 `rationale`에만 들어가고 소스·freshness에 **구조적으로** 연결되지 않는다. `IngestReportItem`에는 비정형 `evidence_snapshot: dict`(`app/schemas/investment_reports.py:236`)와 `cited_snapshot_uuids`만 있고, 타입드 `evidence:[{source, as_of, value}]`나 item-level freshness 필드가 **없다**. → **P1**.
- ❌ `investment_report_context_get(draft_policy="advisory_only")`가 `_ADVISORY_DRAFT_PROFILES = frozenset({"HERMES_ADVISOR"})`(`app/services/investment_reports/query_service.py:43`)만 admit → `CLAUDE_ADVISOR` draft는 다음 리포트 baseline으로 **체이닝 안 됨**. → **P3**.
- ❌ (deferred) 품질 grade/snapshot_* 전부 null(P2), report_item↔order 미링크(P4).

---

## 2. Goals / Non-goals

**Goals**
- (S1) `investment_report_create`를 **1발에** 올릴 수 있게: 계약을 도구 description + CLAUDE.md에 노출하고, 검증 에러를 **한 응답에 모아** 반환.
- (S2) 정당한 `CLAUDE_ADVISOR` draft가 `advisory_only`에서 baseline으로 체이닝되게(스모크 boilerplate는 계속 제외).
- (S3) 항목 근거를 **타입드·검증된** 구조로 실어 소스 연결 가능하게(자유 텍스트 보강).

**Non-goals (이번 라운드 제외)**
- P2: Claude가 snapshot-backed 등급 리포트를 직접 생성 / `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED` 게이트 변경. (대형 + 안전 민감 — default-grade footgun: `build_report_quality_summary(None, None)`이 coverage 0인데도 `grade="high_confidence"` 반환.)
- P4: report_item ↔ order_id 링크(라이브 ledger는 `correlation_id`도 없음 — 교차 절단 변경).
- 브로커/주문/감시/order-intent mutation. **어느 슬라이스도 도달하지 않음.**
- DB 마이그레이션(권장안에서는 셋 다 0; S3 대안만 선택 시 1개).

---

## 3. Slice 1 — ROB-458 (ships first · 1 PR · no migration)

**목표:** 계약 가시화 + 모든 위반 한 번에.

### 3.1 변경
1. **공용 검증 헬퍼 추출** — `generate_from_bundle`(`handlers.py:715-759`)의 item-별 검증 블록을 단일 헬퍼로 추출하여 **두 도구가 공유**(drift 방지). 예:
   ```python
   def _validate_report_items(
       raw_items: list[dict[str, Any]] | None,
   ) -> tuple[list[IngestReportItem], dict[str, Any] | None]:
       """Return (validated_items, error_payload). error_payload is None on success.

       On failure, error_payload mirrors the generate_from_bundle contract:
       {success:False, error:"invalid_items", item_errors:[{index, client_item_key,
       errors:[{field, message}]}], required_fields, enums, notes}.
       """
   ```
   `item_errors`는 각 item의 **모든** `exc.errors()`를 `{field: "loc.join('.')", message: err["msg"]}`로 평탄화(기존 `generate_from_bundle`와 byte-동형).
2. **`investment_report_create_impl` 배선** — `handlers.py:283`의 naive comprehension을 헬퍼 호출로 교체. `error_payload`가 None이 아니면 그 dict를 즉시 return(uncaught `ValidationError` 제거). 성공 시 `validated_items`로 `IngestReportRequest` 구성.
   - 주의: 현재 happy-path는 `report_key` 프로브로 idempotent 여부를 판정한다(`handlers.py:295-305`). 검증은 그 **앞**에서 수행해 잘못된 item이 DB 세션을 열기 전에 차단되게 한다.
3. **`generate_from_bundle`도 헬퍼로 치환** — 동일 동작을 유지하되 중복 제거(회귀 테스트로 byte-동형 보장).
4. **Rich tool description** (`handlers.py:851-856`) — 계약을 인라인 기술:
   - 필수: `client_item_key`, `item_kind`(action|watch|risk), `intent`(buy_review|sell_review|risk_review|trend_recovery_review|rebalance_review), `rationale`.
   - watch 규칙: `item_kind="watch"` & `operation∈{None,create,modify}`이면 `watch_condition` + `valid_until` 필수.
   - **`target_kind`(asset|index|fx, default `asset`)는 별개의 선택 필드** — `item_kind`와 혼동 금지 명시.
   - `decision_bucket`은 DECISION_BUCKETS 어휘.
   - 체이닝을 원하면 `created_by_profile="CLAUDE_ADVISOR"` 권장(S2 연결).
5. **CLAUDE.md 운영노트 신규 추가** — 투자 리포트 섹션에 `investment_report_create` item 계약 + `item_kind` vs `target_kind` 구분을 짧게 기재(전제 정정 반영: "수정"이 아니라 "추가").

### 3.2 테스트
- `_validate_report_items` 단위: (a) 필수 필드 누락 → `item_errors`에 **모든** 위반 동시 노출; (b) 잘못된 enum → `enums` 블록 포함; (c) watch without `watch_condition` → 위반 표면; (d) 복수 item 중 일부만 불량 → 불량 인덱스만; (e) happy path → `(items, None)`.
- `investment_report_create` 통합: 불량 호출이 `{success:False, error:"invalid_items", …}`를 반환하고 **DB write 없음**; 정상 호출은 기존대로 생성/idempotent.
- `generate_from_bundle` 회귀: 기존 에러 응답 byte-동형 유지.

### 3.3 리스크
낮음 — 동작 변화는 에러 **형태**(uncaught `ValidationError` → 구조화 dict)뿐이며, 이는 옆 도구가 이미 반환하는 형태와 동일. DB/마이그레이션 없음.

---

## 4. Slice 2 — ROB-459 P3 · advisory chaining (1 PR · no migration)

**목표:** 정당한 `CLAUDE_ADVISOR` draft를 `advisory_only` baseline에 admit, 스모크 boilerplate(`"t"`, `"test"`, …)는 계속 제외.

### 4.1 변경
1. **기본 화이트리스트 확장** — `query_service.py:43`:
   ```python
   _DEFAULT_ADVISORY_DRAFT_PROFILES: frozenset[str] = frozenset({"HERMES_ADVISOR", "CLAUDE_ADVISOR"})
   ```
2. **config-extendable (union, fail-closed)** — `app/core/config.py`에 `INVESTMENT_ADVISORY_DRAFT_PROFILES: list[str] = []` 추가하고, 기존 `validate_public_api_paths`(`config.py:611-626`) 패턴을 미러한 `field_validator`로 `list[str] | str`(comma-sep)을 파싱. 런타임 화이트리스트 = `_DEFAULT_ADVISORY_DRAFT_PROFILES ∪ set(settings.INVESTMENT_ADVISORY_DRAFT_PROFILES)`. **union만** — 기존 default를 줄이거나 임의 `"all"`을 허용하지 않음(주석의 fail-closed 불변 보존). `_VALID_DRAFT_POLICIES`(여전히 `{exclude, advisory_only}`, **no `all`**)는 불변.
3. **`_is_advisory_draft` / 화이트리스트 소스** — 모듈-상수 직접 참조 대신 런타임 합성(default ∪ config)을 사용하도록 좁게 수정. 합성은 순수 함수(테스트 주입 가능)로.
4. **주석 + 도구 description 갱신** — `query_service.py:37-43` 주석에 CLAUDE_ADVISOR 포함 사유 반영; `investment_report_context_get` 등록 description(`handlers.py:893-905`)의 `advisory_only` 설명을 "HERMES_ADVISOR/CLAUDE_ADVISOR(및 설정된 advisory 프로필) draft admit"으로 갱신.
5. **카논 프로필 문서화** — Claude-authored advisory는 `created_by_profile="CLAUDE_ADVISOR"`를 넘겨야 체이닝됨(S1 description에 이미 노출).

### 4.2 테스트
- `test_investment_reports_query_prior_drafts.py` 확장: (a) `CLAUDE_ADVISOR` draft가 `advisory_only`에서 admit; (b) `"t"`(스모크)는 여전히 제외; (c) `INVESTMENT_ADVISORY_DRAFT_PROFILES`로 커스텀 프로필 추가 시 admit; (d) `exclude` 정책은 여전히 전부 제외; (e) `all` 등 미지원 정책은 기존대로 거부/fallback.

### 4.3 리스크
낮음/additive. 동작 확대는 "정당 advisory 1종 추가"로 한정. 마이그레이션 없음.

---

## 5. Slice 3 — ROB-459 P1 · structured per-item evidence (1 PR · no migration)

**목표:** 항목 근거(컨센/수급/종토)를 타입드·검증된 구조로 실어 소스 연결. 자유 텍스트 `rationale`/`evidence_snapshot` 보강(대체 아님).

### 5.1 변경
1. **`ItemEvidencePayload` 신규 submodel** (`app/schemas/investment_reports.py`):
   ```python
   class ItemEvidencePayload(BaseModel):
       source: str = Field(min_length=1)          # 필수: 'consensus' | 'foreign_flow' | 'forum' | …(자유 문자열)
       metric: str | None = None                  # 예: 'buy_ratings', 'foreign_net', 'overheat_score'
       value: str | Decimal | None = None         # JSON-safe로 직렬화
       as_of: datetime | str | None = None        # ISO8601 또는 자유 라벨
       freshness: ItemEvidenceFreshnessLiteral | None = None  # fresh|soft_stale|stale|unknown (item 근거의 신선도)
   ```
   - `model_config` extra 정책은 `IngestReportItem`과 동일(현재 Pydantic v2 default = ignore). value/as_of는 비강제(advisory 자유도).
2. **`IngestReportItem` 추가 필드(additive)**:
   ```python
   evidence: list[ItemEvidencePayload] = Field(default_factory=list)
   freshness: ItemEvidenceFreshnessLiteral | None = None   # 항목 종합 freshness(선택)
   ```
   기본 빈 리스트/None → **하위 호환**(기존 호출 무영향).
3. **No-migration 영속화** — `_insert_item`(`app/services/investment_reports/ingestion.py:285`)에서, 현재 `evidence_snapshot=item_req.evidence_snapshot`(line 324)로 쓰는 dict에 구조 근거를 **reserved key로 병합**:
   ```python
   merged_evidence = dict(item_req.evidence_snapshot or {})
   if item_req.evidence:
       merged_evidence["structured_evidence"] = [e.model_dump(mode="json") for e in item_req.evidence]
   if item_req.freshness is not None:
       merged_evidence["item_freshness"] = item_req.freshness
   # evidence_snapshot=merged_evidence
   ```
   - insert/overwrite 두 경로 모두 `_insert_item`을 통과하므로 **한 메서드만** 변경.
   - **round-trip 확인됨:** `InvestmentReportItemResponse.evidence_snapshot`(`app/schemas/investment_reports.py:579`)가 그대로 노출 → `investment_report_get`/`_list`에서 구조 근거가 무비용 round-trip.
   - 기존 `evidence_snapshot["action_verdict"]`(action_packet 소비) 등과 **키 충돌 없음**(예약 키 `structured_evidence`/`item_freshness` 신규).
4. **description 노출** — S1의 enriched description에 선택 필드 `evidence[]`/`freshness` 한 줄 추가.

### 5.2 대안 (SQL-queryable가 필요하면)
전용 `item_evidence` JSONB 컬럼 + alembic 마이그레이션(additive nullable). 소스별 SQL 조회가 필요할 때만. **권장은 no-migration(위 §5.1)** — 사용자 선호(additive/migration-min) + round-trip 무비용.

### 5.3 테스트
- create→get round-trip: `evidence`/`freshness`가 `evidence_snapshot.structured_evidence`/`item_freshness`로 저장·노출.
- 검증: `source` 누락 → S1의 batched item-error 경로로 표면(`item_errors`에 `evidence.0.source` 노출).
- 생략/빈 리스트: 기존 동작 무변화(키 미추가).
- `value` Decimal/문자열 모두 JSON-safe 직렬화 확인(기존 watch_condition `mode="json"` 패턴과 동일).

### 5.4 리스크
낮음/additive. 권장안은 마이그레이션 없음. 기존 evidence_snapshot 소비자 무영향(신규 예약 키만 추가).

---

## 6. 교차 절단 제약 / 롤아웃

- **mutation 없음:** 어느 슬라이스도 브로커/주문/감시/order-intent를 건드리지 않음. 전부 read-mostly 또는 additive schema.
- **마이그레이션:** 권장안 셋 다 0. (S3 대안만 1개 — 채택 시 operator `alembic upgrade head` 게이트.)
- **default-off 불필요:** S1은 에러 UX 개선(즉시), S2/S3는 additive 계약 확장(즉시). 신규 env(`INVESTMENT_ADVISORY_DRAFT_PROFILES`)는 미설정 시 default 동작.
- **순서:** S1 머지 → S2 → S3. 각 PR은 최신 `origin/main` 기준 새 브랜치. S1의 enriched description이 S2(카논 프로필)·S3(evidence 필드) 노출의 공통 표면이라 **S1 먼저**가 자연 의존.
- **CI/회귀:** `generate_from_bundle` 에러 응답 byte-동형, advisory draft 쿼리, evidence round-trip을 테스트로 고정.

## 7. Deferred (별도 후속 이슈)

- **ROB-459 P2** — Claude-runnable graded reports: lite grade 산출(footgun 해소: 근거 부재 시 `high_confidence` 금지) 또는 generator gate를 Claude 프로필에 개방. 안전/제품 결정 동반.
- **ROB-459 P4** — report_item ↔ order_id 링크: `report_item_uuid`를 ledger/order 도구에 스레딩(라이브 ledger `correlation_id` 부재부터 해소). `WatchOrderIntentLedger`(ROB-402/404) correlation_id 선례 참고.

## 8. 핵심 코드 앵커 (grounding)

| 무엇 | 위치 |
|---|---|
| create 핸들러 시그니처(`items: list[dict]`) | `app/mcp_server/tooling/investment_reports_handlers.py:248` |
| naive 검증(첫 실패에 throw) | `…handlers.py:283` |
| 정답 패턴(batched item_errors + enums) | `…handlers.py:715-759` |
| create 등록 description(bare) | `…handlers.py:851-857` |
| context_get 등록 description | `…handlers.py:893-905` |
| `IngestReportItem` / enums / evidence_snapshot | `app/schemas/investment_reports.py:217-256` (evidence_snapshot:236) |
| `InvestmentReportItemResponse.evidence_snapshot`(round-trip) | `app/schemas/investment_reports.py:579` |
| advisory 화이트리스트 | `app/services/investment_reports/query_service.py:43`, 필터 `:307-313` |
| item ORM 영속화(`_insert_item`) | `app/services/investment_reports/ingestion.py:285`, evidence_snapshot write `:324` |
| config comma-sep 검증 선례 | `app/core/config.py:611-626` |
