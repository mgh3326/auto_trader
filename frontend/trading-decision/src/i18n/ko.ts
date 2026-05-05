import type {
  ActionKind,
  CommitteeAccountMode,
  ExecutionAccountMode,
  ExecutionReviewStageStatus,
  ExecutionSource,
  InstrumentType,
  LinkDirection,
  OutcomeHorizon,
  PreopenArtifactReadinessStatus,
  PreopenArtifactStatus,
  PreopenNewsReadinessStatus,
  PreopenPaperApprovalBridgeStatus,
  PreopenPaperApprovalCandidateStatus,
  PreopenQaCheckSeverity,
  PreopenQaCheckStatus,
  PreopenQaConfidence,
  PreopenQaEvaluatorStatus,
  PreopenQaGrade,
  ProposalKind,
  ResearchSessionStatus,
  SessionStatus,
  Side,
  StageType,
  StageVerdict,
  StrategyEventType,
  SummaryDecision,
  TrackKind,
  UserResponseValue,
  WorkflowStatus,
} from "../api/types";
import type {
  CandidateKind,
  NxtClassification,
  ReconciliationStatus,
} from "../api/reconciliation";

export const SESSION_STATUS_LABEL: Record<SessionStatus, string> = {
  open: "진행 중",
  closed: "종료",
  archived: "보관됨",
};

export const USER_RESPONSE_LABEL: Record<UserResponseValue, string> = {
  pending: "대기",
  accept: "수락",
  partial_accept: "부분 수락",
  modify: "수정",
  defer: "보류",
  reject: "거절",
};

export const RESPONSE_BUTTON_LABEL: Record<
  "accept" | "partial_accept" | "modify" | "defer" | "reject",
  string
> = {
  accept: "수락",
  partial_accept: "부분 수락",
  modify: "수정",
  defer: "보류",
  reject: "거절",
};

export const SIDE_LABEL: Record<Side, string> = {
  buy: "매수",
  sell: "매도",
  none: "—",
};

export const PROPOSAL_KIND_LABEL: Record<ProposalKind, string> = {
  trim: "축소",
  add: "추가 매수",
  enter: "신규 진입",
  exit: "청산",
  pullback_watch: "되돌림 관찰",
  breakout_watch: "돌파 관찰",
  avoid: "회피",
  no_action: "무행동",
  other: "기타",
};

export const ACTION_KIND_LABEL: Record<ActionKind, string> = {
  live_order: "실주문",
  paper_order: "모의주문",
  watch_alert: "감시 알림",
  no_action: "무행동",
  manual_note: "수기 메모",
};

export const TRACK_KIND_LABEL: Record<TrackKind, string> = {
  accepted_live: "수락(실주문)",
  accepted_paper: "수락(모의)",
  rejected_counterfactual: "거절 대조",
  analyst_alternative: "분석가 대안",
  user_alternative: "사용자 대안",
};

export const OUTCOME_HORIZON_LABEL: Record<OutcomeHorizon, string> = {
  "1h": "1시간",
  "4h": "4시간",
  "1d": "1일",
  "3d": "3일",
  "7d": "7일",
  final: "최종",
};

export const INSTRUMENT_TYPE_LABEL: Record<InstrumentType, string> = {
  equity_kr: "국내주식",
  equity_us: "해외주식",
  crypto: "암호화폐",
  forex: "외환",
  index: "지수",
};

export const ACCOUNT_MODE_LABEL: Record<CommitteeAccountMode, string> = {
  kis_mock: "KIS 모의",
  alpaca_paper: "Alpaca Paper",
  kis_live: "KIS 실계좌",
  db_simulated: "DB 시뮬레이션",
};

export const EXECUTION_ACCOUNT_MODE_LABEL: Record<ExecutionAccountMode, string> = {
  kis_live: "KIS 실계좌",
  kis_mock: "KIS 모의",
  alpaca_paper: "Alpaca Paper",
  db_simulated: "DB 시뮬레이션",
};

export const EXECUTION_SOURCE_LABEL: Record<ExecutionSource, string> = {
  preopen: "장전",
  watch: "감시",
  manual: "수기",
  websocket: "실시간",
  reconciler: "조정",
};

export const EXECUTION_REVIEW_STAGE_STATUS_LABEL: Record<
  ExecutionReviewStageStatus,
  string
> = {
  ready: "준비 완료",
  degraded: "주의",
  unavailable: "미사용",
  skipped: "건너뜀",
  pending: "대기",
};

export const WORKFLOW_STATUS_LABEL: Record<WorkflowStatus, string> = {
  created: "생성됨",
  evidence_generating: "근거 수집 중",
  evidence_ready: "근거 준비됨",
  debate_ready: "토론 준비됨",
  trader_draft_ready: "트레이더 초안 준비",
  risk_review_ready: "리스크 리뷰 준비",
  auto_approved: "자동 승인",
  preview_ready: "프리뷰 준비",
  journal_ready: "기록 준비",
  completed: "완료",
  failed_evidence: "근거 실패",
  failed_trader_draft: "트레이더 초안 실패",
  failed_risk_review: "리스크 리뷰 실패",
  preview_blocked: "프리뷰 차단",
};

export const RECONCILIATION_STATUS_LABEL: Record<ReconciliationStatus, string> = {
  maintain: "유지",
  near_fill: "체결 임박",
  too_far: "괴리 큼",
  chasing_risk: "추격 위험",
  data_mismatch: "데이터 불일치",
  kr_pending_non_nxt: "국내 브로커 전용",
  unknown_venue: "거래소 알 수 없음",
  unknown: "알 수 없음",
};

export const NXT_CLASSIFICATION_LABEL: Record<NxtClassification, string> = {
  buy_pending_at_support: "매수 대기(지지선 근접)",
  buy_pending_too_far: "매수 대기(괴리 큼)",
  buy_pending_actionable: "매수 대기(실행 가능)",
  sell_pending_near_resistance: "매도 대기(저항선 근접)",
  sell_pending_too_optimistic: "매도 대기(낙관 과다)",
  sell_pending_actionable: "매도 대기(실행 가능)",
  non_nxt_pending_ignore_for_nxt: "비-NXT 대기",
  holding_watch_only: "보유 감시",
  data_mismatch_requires_review: "데이터 불일치 검토 필요",
  unknown: "알 수 없음",
};

export const CANDIDATE_KIND_LABEL: Record<CandidateKind, string> = {
  pending_order: "대기 주문",
  holding: "보유",
  screener_hit: "스크리너 적중",
  proposed: "제안됨",
  other: "기타",
};

export const NEWS_READINESS_LABEL: Record<PreopenNewsReadinessStatus, string> = {
  ready: "정상",
  stale: "오래됨",
  unavailable: "미사용",
};

export const ARTIFACT_STATUS_LABEL: Record<PreopenArtifactStatus, string> = {
  unavailable: "미사용",
  draft: "초안",
  ready: "준비 완료",
  degraded: "주의",
};

export const ARTIFACT_READINESS_LABEL: Record<
  PreopenArtifactReadinessStatus,
  string
> = {
  ready: "준비 완료",
  stale: "오래됨",
  unavailable: "미사용",
  partial: "일부",
};

export const PAPER_APPROVAL_STATUS_LABEL: Record<
  PreopenPaperApprovalBridgeStatus,
  string
> = {
  available: "사용 가능",
  warning: "주의",
  blocked: "차단됨",
  unavailable: "미사용",
};

export const PAPER_APPROVAL_CANDIDATE_STATUS_LABEL: Record<
  PreopenPaperApprovalCandidateStatus,
  string
> = {
  available: "사용 가능",
  warning: "주의",
  unavailable: "미사용",
};

export const QA_STATUS_LABEL: Record<PreopenQaEvaluatorStatus, string> = {
  ready: "준비 완료",
  needs_review: "검토 필요",
  unavailable: "미사용",
  skipped: "건너뜀",
};

export const QA_CHECK_STATUS_LABEL: Record<PreopenQaCheckStatus, string> = {
  pass: "통과",
  warn: "주의",
  fail: "실패",
  unknown: "알 수 없음",
  skipped: "건너뜀",
};

export const QA_SEVERITY_LABEL: Record<PreopenQaCheckSeverity, string> = {
  info: "정보",
  low: "낮음",
  medium: "보통",
  high: "높음",
};

export const QA_GRADE_LABEL: Record<PreopenQaGrade, string> = {
  excellent: "매우 우수",
  good: "양호",
  watch: "주의",
  poor: "미흡",
  unavailable: "미사용",
};

export const QA_CONFIDENCE_LABEL: Record<PreopenQaConfidence, string> = {
  high: "높음",
  medium: "보통",
  low: "낮음",
  unavailable: "미사용",
};

export const VENUE_LABEL: Record<string, string> = {
  upbit: "Upbit",
  alpaca_paper: "Alpaca Paper",
  kis: "KIS",
  kis_mock: "KIS 모의",
  kis_live: "KIS 실계좌",
};

export const WARNING_TOKEN_LABEL: Record<string, string> = {
  missing_quote: "시세 누락",
  stale_quote: "시세 오래됨",
  missing_orderbook: "호가 누락",
  missing_support_resistance: "지지/저항선 미사용",
  missing_kr_universe: "국내 유니버스 누락",
  non_nxt_venue: "비-NXT 거래소",
  unknown_venue: "거래소 알 수 없음",
  unknown_side: "방향 알 수 없음",
};

export const SAFETY_SCOPE_LABEL: Record<string, string> = {
  preview_only_confirm_false_no_broker_submit:
    "브로커 제출 없는 preview 전용",
  advisory_only: "자문 전용",
};

export const PURPOSE_LABEL: Record<string, string> = {
  paper_plumbing_smoke: "페이퍼 배관 점검",
  alpha_candidate_review: "알파 후보 리뷰",
};

export const STRATEGY_EVENT_TYPE_LABEL: Record<StrategyEventType, string> = {
  operator_market_event: "운영자 시장 이벤트",
  earnings_event: "실적 이벤트",
  macro_event: "매크로 이벤트",
  sector_rotation: "섹터 로테이션",
  technical_break: "기술적 돌파",
  risk_veto: "리스크 거부",
  cash_budget_change: "현금 예산 변경",
  position_change: "포지션 변경",
};

export const COMMON = {
  dash: "—",
  loading: "불러오는 중…",
  saving: "저장 중…",
  retry: "다시 시도",
  cancel: "취소",
  refresh: "새로고침",
  next: "다음",
  previous: "이전",
  all: "전체",
  yes: "예",
  no: "아니오",
  unknown: "알 수 없음",
  rawData: "원본 데이터 보기",
  somethingWentWrong: "오류가 발생했습니다. 다시 시도해 주세요.",
} as const;

export const STAGE_TYPE_LABEL: Record<StageType, string> = {
  market: "시장",
  news: "뉴스",
  fundamentals: "펀더멘털",
  social: "소셜",
};

export const STAGE_VERDICT_LABEL: Record<StageVerdict, string> = {
  bull: "강세",
  bear: "약세",
  neutral: "중립",
  unavailable: "준비 중",
};

export const SUMMARY_DECISION_LABEL: Record<SummaryDecision, string> = {
  buy: "매수",
  hold: "보유",
  sell: "매도",
};

export const LINK_DIRECTION_LABEL: Record<LinkDirection, string> = {
  support: "지지",
  contradict: "반대",
  context: "맥락",
};

export const RESEARCH_SESSION_STATUS_LABEL: Record<
  ResearchSessionStatus,
  string
> = {
  open: "열림",
  running: "분석 중",
  finalized: "완료",
  failed: "실패",
  cancelled: "취소됨",
};

export const RESEARCH_TAB_LABEL = {
  summary: "종합",
  market: "시장",
  news: "뉴스",
  fundamentals: "펀더멘털",
  social: "소셜",
  raw: "원본",
} as const;

export const RESEARCH_INSTRUMENT_TYPE_LABEL = {
  equity_kr: "국내주식",
  equity_us: "해외주식",
  crypto: "암호화폐",
} as const;
