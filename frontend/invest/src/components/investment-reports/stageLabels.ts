// ROB-279 Phase 5 — Korean labels for stage artifact types and verdicts.
//
// Centralised so a new StageType or StageVerdict value only needs to be
// reflected in one place.

import type { StageType, StageVerdict } from "../../types/investmentReports";

export const STAGE_TYPE_LABELS: Record<StageType, string> = {
  market: "시장",
  news: "뉴스",
  portfolio_journal: "포트폴리오·저널",
  watch_context: "와치 컨텍스트",
  candidate_universe: "후보 종목",
  bull_reducer: "매수 측 요약",
  bear_reducer: "매도/위험 측 요약",
  risk_review: "리스크 리뷰",
};

export const VERDICT_LABELS: Record<StageVerdict, string> = {
  bull: "매수 측",
  bear: "매도 측",
  neutral: "중립",
  unavailable: "확인 불가",
};
