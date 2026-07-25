# ROB-664 — /invest 분석 아티팩트 뷰어 + 세션 컨텍스트 타임라인 (read-only 웹 노출)

**Status:** Design approved (2026-07-03)
**Priority:** Low
**Linear:** ROB-664 (follow-up to ROB-662 회고 browser / ROB-663 forecast dashboard)

## 배경

`review.analysis_artifacts` (ROB-637/648: kind·readiness_label·content_hash·version·valid_until TTL)
와 `review.operator_session_context` (append-only 핸드오프 로그, entry_type 8종) 모두 **HTTP 노출 0**.
"오늘 어떤 분석이 이미 됐고 뭐가 stale인지", "지난 세션이 뭘 남겼는지"를 웹에서 못 봄 —
중복 분석 방지 힌트(`fresh_artifact_exists`) 디버깅에도 필요.

## 목표 / 비목표

**목표 (read-only, migration 0):**
1. 아티팩트 뷰어: market/kind/readiness_label/symbol 필터 + `is_stale` 배지 + version/content_hash
   표시 + payload JSON 뷰어(상세에서만 로드).
2. 세션 컨텍스트 타임라인: kst_date/market별 최근 엔트리, entry_type 칩. "최근 핸드오프" 카드.

**비목표:**
- 쓰기/mutation 일체 없음. 브로커/주문/감시 도달 없음.
- 신규 DB 마이그레이션 없음 (migration 0).
- 신규 서비스 쿼리 메서드 없음 — 기존 read 메서드 재사용 (아래 1개 필터만 additive 확장).

## 안전 경계

read-only, 서비스 레이어 경유, migration 0. ROB-662/663 패턴 그대로 미러.

## 아키텍처

### 데이터 소스 (기존, 변경 없음)
- 모델: `app/models/analysis_artifact.py::AnalysisArtifact` (`is_stale` = valid_until < now_kst()),
  `app/models/session_context.py::OperatorSessionContext` (append-only, expiry 없음).
- 서비스:
  - `app/services/analysis_artifact.py::AnalysisArtifactService`
    — `list_artifacts(...)` (as_of DESC, staleness 필터), `get(id|uuid)`. **읽기 메서드 이미 존재.**
  - `app/services/session_context.py::SessionContextService`
    — `get_recent(...)` (created_at DESC, market/account_scope/kst_date_from/entry_type 필터).
      **읽기 메서드 이미 존재.**
- 스키마 (기존 재사용): `app/schemas/analysis_artifact.py` (`AnalysisArtifactMeta`(payload 없음, is_stale/version/
  content_hash/payload_size_bytes), `AnalysisArtifactRead`(+payload), `AnalysisArtifactListResponse`,
  `AnalysisArtifactGetResponse`), `app/schemas/session_context.py` (`SessionContextResponse`,
  `SessionContextRecentResponse`). 모두 `from_attributes=True` (ORM 모드).

### 서비스 변경 (유일한 백엔드 데이터 변경)
`AnalysisArtifactService.list_artifacts`에 **optional `readiness_label` 필터** 추가 (default `None`).
ticket이 요구하는 필터이며 현재 없는 유일한 gap. additive — 시그니처 확장, 없으면 기존 동작 불변.
`AnalysisArtifactListRequest` 스키마에 이미 readiness 관련 필드가 있는지 구현 시 확인하여 정합.

### 라우터 (신규, thin GET — ROB-663 미러)

**A. `app/routers/invest_artifacts.py`** — prefix `/trading/api/invest/artifacts`
- `GET /` → `list_artifacts(market, kind, readiness_label, symbol, include_stale, limit)`
  → **metadata only** (`AnalysisArtifactListResponse`, payload 제외 — ROB-504 교훈).
- `GET /{artifact_id}` → `get(...)` → **payload 포함** (`AnalysisArtifactGetResponse`);
  404 if None. expand-on-demand JSON 뷰어용.
- 잘못된 `kind`/`readiness_label` enum → HTTP 422 (ROB-663 `_validate_*` 패턴).

**B. `app/routers/invest_session_context.py`** — prefix `/trading/api/invest/session-context`
- `GET /recent` → `get_recent(market, account_scope, entry_type, kst_date_from, limit)`
  → `SessionContextRecentResponse`.
- 잘못된 `entry_type`/`market`/`account_scope` enum → HTTP 422.

공통: `Depends(get_authenticated_user)` + `Depends(get_db)`, per-request 서비스 인스턴스화.
`app/main.py` invest 클러스터(L37 import 그룹 + L193-201 include)에 2개 라우터 등록.

### 프런트엔드 (`frontend/invest/src`, 둘 다 `/invest/insights`)
- `types/analysisArtifacts.ts`, `types/sessionContext.ts` — Pydantic 스키마 snake_case 필드 미러.
- `api/analysisArtifacts.ts` (`fetchArtifacts(list)` + `fetchArtifactDetail(byId)`),
  `api/sessionContext.ts` (`fetchRecentSessionContext`). `credentials:"include"`, camelCase 쿼리 키.
- `components/insights/AnalysisArtifactPanel.tsx` — market/kind/readiness 필터 칩, `is_stale` 배지
  (Pill "danger" tone), version/content_hash 표시, row 클릭 → detail fetch → payload JSON 펼침.
  `data-testid="analysis-artifact-panel"`.
- `components/insights/SessionContextTimelinePanel.tsx` — 최근 엔트리 리스트, entry_type 칩
  (plan/decision/handoff_note 등 8종), title/body, market·kst_date 그룹. "최근 핸드오프" 카드.
  `data-testid="session-context-timeline-panel"`.
- `pages/desktop/DesktopInsightsPage.tsx` — `ForecastCalibrationPanel` 옆 그리드에 두 패널 배선.

## 테스트

- 백엔드 unit: `tests/routers/test_invest_artifacts_router.py`,
  `tests/routers/test_invest_session_context_router.py`
  — 서비스 monkeypatch, `dependency_overrides`로 auth/db 스텁 (ROB-663 `_make_client` 패턴).
  기본값/필터 전달/422/응답 envelope 검증. list가 payload 미포함인지 assert.
- 백엔드 integration: `tests/test_analysis_artifact_web_read.py`
  (신규 `readiness_label` 필터 + is_stale + 정렬 커버), `tests/test_session_context_web_read.py`.
  `pytest.mark.integration` + cleanup fixture (ROB-663 `test_forecast_web_read.py` 패턴).
- 프런트: `frontend/invest/src/__tests__/AnalysisArtifactPanel.test.tsx`,
  `SessionContextTimelinePanel.test.tsx` — vitest, fetch stub, MemoryRouter.

## 결정 기록 (design review)

- **2개 라우터 파일** (통합 1개 아님) — 리소스별 격리, 독립 테스트 용이.
- **기존 MCP-era 스키마 재사용** (신규 DTO 아님) — 코드 최소화, ORM 모드 자연 적합.
- **프런트 위치 = /insights** — ROB-662/663이 쓴 read-only 관찰 surface. 신규 /workspace 페이지는
  Low 우선순위 대비 과함 (user 확인).
- **단일 PR** — 두 표면 함께 (ROB-662/663 선례, user 확인).

## 마이그레이션

없음 (migration 0).
