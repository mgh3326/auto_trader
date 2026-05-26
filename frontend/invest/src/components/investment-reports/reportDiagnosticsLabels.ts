// ROB-318 Phase 3 (PR-C) — Korean labels for the report-diagnostics panel.
//
// Centralised so the closed enums from the backend
// (app/services/action_report/common/diagnostics.py) map to operator-facing
// Korean in one place. Status labels reuse the shared FRESHNESS_LABELS.

import type {
  ReportQualityGrade,
  WhyNoActionKind,
} from "../../types/investmentReports";

export const QUALITY_GRADE_LABELS: Record<ReportQualityGrade, string> = {
  high_confidence: "높음",
  informational_only: "정보용",
  no_action: "액션 불가",
};

export const WHY_NO_ACTION_LABELS: Record<WhyNoActionKind, string> = {
  data_insufficient: "데이터 부족",
  stale_gated: "데이터 오래됨",
  real_no_action: "관망 (신규 액션 없음)",
};

// Closed ReasonCode enum → Korean. Unknown codes fall back to the raw value.
export const REASON_CODE_LABELS: Record<string, string> = {
  user_id_missing: "사용자 미지정",
  kis_fetch_failed: "KIS 조회 실패",
  stale: "오래됨",
  unavailable: "확인 불가",
  failed: "실패",
  unknown: "원인 미상",
};

// Per-kind Korean labels (mirror snapshot_kind on the backend).
export const DIAGNOSTIC_KIND_LABELS: Record<string, string> = {
  portfolio: "포지션",
  journal: "거래일지",
  watch_context: "감시",
  market: "시장",
  news: "뉴스",
  candidate_universe: "후보군",
  symbol: "종목",
  // ROB-323 — external cross-check sources.
  toss_remote_debug: "토스증권 교차검증",
  naver_remote_debug: "네이버증권 교차검증",
  browser_probe: "브라우저 교차검증",
};

// ROB-323 — external cross-check section copy.
export const EXTERNAL_CROSS_CHECK_TITLE = "외부 교차검증";
export const EXTERNAL_CROSS_CHECK_NOTE = "리포트 생성에는 영향 없음";
