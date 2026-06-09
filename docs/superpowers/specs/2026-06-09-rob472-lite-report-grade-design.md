# ROB-472 — Claude lite 리포트의 결정적 품질 grade (Design Spec)

- **Issue:** [ROB-472](https://linear.app/mgh3326/issue/ROB-472) (Feature, ROB-459 P2에서 분리)
- **Date:** 2026-06-09
- **Status:** Approved design → writing-plans
- **Scope:** 1 슬라이스 (1 PR). additive · migration 0 · schema 변경 0 · broker/order/watch mutation 0.

---

## 1. 배경 (코드-grounded, 현재 main `74dee6ee`)

Claude가 `investment_report_create`(lite create)로 올린 advisory 리포트는 `snapshot_*`/품질 `grade`가 전부 null이다. HERMES generator 리포트(`snapshot_backed_advisory_v1`)는 `snapshot_report_diagnostics.report_quality_summary.grade`를 갖는 것과 대비 — Claude 리포트는 품질 신호가 없어 동급 신뢰도를 못 가진다.

### 1-1. ⚠️ 전제 정정 (footgun은 실재하지 않음)
ROB-472/ROB-459 P2 본문은 `build_report_quality_summary(freshness_summary=None, bundle_status=None)`이 **coverage 0인데도 `high_confidence`를 반환**한다고 했으나, 라인 단위 추적 결과 **거짓**이다. 빈 입력이면 `thin_coverage`(internal 0% < `HIGH_CONFIDENCE_MIN_COVERAGE_PCT`=70)가 발동해 **`informational_only`를 반환**한다 (`app/services/action_report/common/diagnostics.py:360,376-378`). 이 함수는 **이미 빈 입력에 fail-closed**다. 따라서 "근거 없는데 high_confidence로 위장" 시나리오는 없으며, 본 설계는 그 정직성을 *구조적으로* 더 강하게 보장한다(lite는 `high_confidence` 자체가 불가).

### 1-2. grounded 사실
- **grade는 display/audit 메타데이터 전용** — 어떤 백엔드도 grade로 생성/발행을 게이트하지 않음(`diagnostics.py:301`). lite-grade 추가는 순수 정보성·저위험.
- **lite create seam이 깨끗**: `investment_report_create_impl`(`app/mcp_server/tooling/investment_reports_handlers.py:420`에서 `IngestReportRequest` 구성 → `:441` `service.ingest`). `snapshot_report_diagnostics`를 포함한 snapshot_* 필드는 **이미 optional**(`app/schemas/investment_reports.py:403` request / `:610` response)이고 ingestion→DB→response로 round-trip. **service/schema/DB 변경 불필요.**
- **DB CHECK 주의**: published 리포트가 `snapshot_freshness_summary`를 세팅하면 `overall ∈ {fresh,soft_stale,partial}` 필수. → lite는 freshness_summary를 **비워두고** `snapshot_report_diagnostics.report_quality_summary`만 채운다(legacy NULL 허용 + 스냅샷 데이터 위조 방지).
- **gate 개방은 과대**: `generate_from_bundle`/`SnapshotBackedReportGenerator.generate`는 심볼당 라이브 KIS/Upbit API 호출 + user_id 포트폴리오 수집 + bundle ensure. lite/캐시 경로 없음 → "Claude에 gate 개방"은 무겁고 안전 게이트 의미 변경 동반. **채택 안 함.**
- **P1 evidence가 입력**: item `evidence: list[ItemEvidencePayload]`(`source` 필수, `freshness∈{fresh,soft_stale,stale,unknown}`) + item `freshness`(동일 literal)가 `evidence_snapshot.structured_evidence`/`item_freshness`에 저장됨(ROB-459 P1, 머지됨). 단 snapshot `FreshnessStatus`(`fresh|soft_stale|hard_stale|partial|unavailable`)와 literal이 다름 → **자체 집계 규칙** 필요(snapshot 로직 직접 재사용 불가).
- **advisory 프로파일 셋 존재**: `query_service._advisory_draft_profiles()`(default `{HERMES_ADVISOR, CLAUDE_ADVISOR}` ∪ config) — 재사용.

---

## 2. Goals / Non-goals

**Goals**
- Claude advisory lite 리포트가 **결정적·정직한 품질 grade**를 갖게 한다 (P1 evidence 기반).
- 정직성: lite는 **`high_confidence` 절대 불가**(스냅샷 coverage 없음) — footgun을 설계로 제거.
- `investment_report_get`으로 round-trip되어 운영자/Hermes가 품질 신호를 읽을 수 있게.

**Non-goals**
- generator gate(`SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED`) 개방 / 라이브 스냅샷 수집. (Non-goal — 무거움/안전.)
- snapshot_freshness_summary / coverage_summary 채우기(스냅샷 위조 금지).
- broker/order/watch/order-intent mutation. DB 마이그레이션. 스키마 필드 추가(이미 존재).
- report_item↔order linkage (= ROB-473, 별도).

---

## 3. 설계

### 3-1. 컴포넌트 — 순수 헬퍼 (신규)
**`app/services/investment_reports/lite_grade.py`** — 순수 함수, DB·IO 없음:

```python
def build_lite_report_quality_summary(items: list[IngestReportItem]) -> dict[str, Any]:
    ...
```

**grade 규칙 (2-레벨, `high_confidence` 부재):**
- `no_action` — actionable item(`item_kind∈{action,watch}`) 없음 **또는** 어떤 item도 structured evidence 없음(근거 부재).
- `informational_only` — 그 외(근거 있는 advisory의 정직한 상한).
- `high_confidence`는 **반환 경로 없음**(lite는 스냅샷 coverage가 없으므로).

**반환 shape** (snapshot용 8-key와 별개, grade Literal 어휘는 공유):
```json
{
  "grade": "informational_only",
  "basis": "item_evidence_lite",
  "reason": "evidence-backed lite advisory (no snapshot coverage)",
  "total_item_count": 3,
  "actionable_item_count": 2,
  "evidence_item_count": 2,
  "evidence_source_count": 4,
  "freshness_breakdown": {"fresh": 3, "soft_stale": 1, "stale": 0, "unknown": 0}
}
```
- `freshness_breakdown`은 **item.freshness + evidence[].freshness 둘 다** 집계(None은 미카운트).
- `evidence_source_count`는 distinct `evidence[].source`.

### 3-2. 배선 + 범위 (handlers.py)
`investment_report_create_impl`에서 `request = IngestReportRequest.model_validate(payload)`(`:420`) **직후**, `async with`(`:422`) **전**에 1줄 주입:

```python
request = _maybe_attach_lite_quality(request)
```

헬퍼(같은 모듈 또는 lite_grade.py):
```python
def _maybe_attach_lite_quality(request: IngestReportRequest) -> IngestReportRequest:
    # caller가 이미 diagnostics를 넘겼으면 존중(clobber 금지)
    if request.snapshot_report_diagnostics is not None:
        return request
    # advisory 프로파일만(스모크/테스트/비-advisory 제외)
    if request.created_by_profile not in _advisory_draft_profiles():
        return request
    try:
        summary = build_lite_report_quality_summary(request.items)
    except Exception:
        return request  # fail-open: grade는 optional 메타데이터, 생성 차단 금지
    return request.model_copy(
        update={"snapshot_report_diagnostics": {"report_quality_summary": summary}}
    )
```
- `_advisory_draft_profiles`는 `app/services/investment_reports/query_service`에서 import 재사용.
- **snapshot_freshness_summary/coverage_summary 미설정**(None 유지) → DB CHECK 안전.

### 3-3. 데이터 흐름
`investment_report_create(items+evidence, created_by_profile="CLAUDE_ADVISOR")` → item 검증 → `_maybe_attach_lite_quality` → `snapshot_report_diagnostics={report_quality_summary:{grade, basis, ...}}` → `ingest` → DB → `investment_report_get`이 `snapshot_report_diagnostics` 반환.

### 3-4. 에러 처리
- **fail-open**: 헬퍼 예외 시 diagnostics 미주입, 리포트 생성 정상 진행.
- **clobber 금지**: caller가 `snapshot_report_diagnostics`를 넘긴 경우 미변경.
- **비-advisory 무영향**: 프로파일이 advisory 셋에 없으면 기존과 동일(diagnostics None).

---

## 4. 테스트
- **헬퍼 단위** (`tests/test_investment_report_lite_grade.py`, 순수/no-DB):
  - actionable item 없음(risk만) → `no_action`.
  - evidence 전무(actionable 있으나 evidence 없음) → `no_action`(reason="no structured evidence...").
  - evidence 있는 action item → `informational_only`.
  - **`high_confidence` 절대 미반환**(어떤 입력에도) — 명시 단언.
  - `freshness_breakdown`(item.freshness + evidence.freshness 합산) / `evidence_source_count`(distinct) 정확.
  - `basis == "item_evidence_lite"`.
- **핸들러 통합** (`tests/mcp_server/test_investment_report_create_handler.py` 확장 또는 DB 테스트):
  - `created_by_profile="CLAUDE_ADVISOR"` + evidence item → 저장된 리포트의 `snapshot_report_diagnostics.report_quality_summary.grade == "informational_only"`.
  - `created_by_profile="t"`(스모크) → `snapshot_report_diagnostics is None`.
  - caller가 `snapshot_report_diagnostics`를 넘기면 미clobber.
  - round-trip: `investment_report_get`이 lite grade 노출.

---

## 5. 제약 / 롤아웃
- **additive · migration 0 · schema 변경 0**(`snapshot_report_diagnostics` 이미 존재) · **mutation 0**.
- default-off 플래그 불필요(순수 메타데이터, 게이트 아님, advisory-only로 범위 제한). 즉시 동작.
- 머지 후 operator 액션 없음.

## 6. 핵심 코드 앵커
| 무엇 | 위치 |
|---|---|
| 빈 입력→informational_only (footgun 부재) | `app/services/action_report/common/diagnostics.py:360,376-378` |
| grade=display-only | `diagnostics.py:301` |
| create seam (request→ingest) | `app/mcp_server/tooling/investment_reports_handlers.py:420,441` |
| snapshot_report_diagnostics (req/resp, optional) | `app/schemas/investment_reports.py:403,610` |
| advisory 프로파일 셋 | `app/services/investment_reports/query_service.py:61` `_advisory_draft_profiles()` |
| P1 evidence/freshness | `app/schemas/investment_reports.py` `ItemEvidencePayload` / `IngestReportItem.evidence,freshness` (literals fresh\|soft_stale\|stale\|unknown) |
| evidence 영속화 | `app/services/investment_reports/ingestion.py:381-391` |
