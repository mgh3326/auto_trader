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
  unknown_side: "方向 알 수 없음",
};

export const SAFETY_SCOPE_LABEL: Record<string, string> = {
  preview_only_confirm_false_no_broker_submit: "브로커 제출 없는 preview 전용",
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

export const portfolioActions = {
  pageTitle: "보유 종목 액션 보드",
  pageSubtitle: "보유 종목별 매도/축소/유지/추가/관망 후보 분류",
  filterMarket: "시장",
  filterAll: "전체",
  filterAction: "액션",
  marketKR: "국내",
  marketUS: "해외",
  marketCRYPTO: "암호화폐",
  actionSell: "전량 정리",
  actionTrim: "부분 축소",
  actionHold: "유지",
  actionAdd: "추가매수",
  actionWatch: "관망",
  colSymbol: "종목",
  colWeight: "비중",
  colProfit: "손익률",
  colDecision: "Research",
  colVerdict: "Market",
  colSupport: "지지 거리",
  colResistance: "저항 거리",
  colJournal: "Journal",
  colReasons: "근거",
  colMissing: "누락 컨텍스트",
  reasonOverweight: "비중 과대",
  reasonUnderweight: "비중 과소",
  reasonResearchBullish: "리서치 매수 의견",
  reasonResearchBearish: "리서치 매도 의견",
  reasonResearchNotBullish: "리서치 매수 아님",
  reasonResearchMissing: "리서치 없음",
  reasonNearResistance: "저항 근접",
  reasonNearSupport: "지지 근접",
  missingJournal: "Journal 미작성",
  missingStakedUnknown: "스테이킹 수량 미상",
  linkResearch: "Research 보기",
  linkOrderPreview: "주문 Preview",
  empty: "보유 종목이 없습니다.",
  loadError: "데이터를 불러오지 못했습니다.",
  warningPrefix: "주의",
};

export const candidates = {
  pageTitle: "신규매수 후보 Discovery",
  pageSubtitle: "screen_stocks 결과를 검토하고 Research Session으로 이어갑니다.",
  filterMarket: "시장",
  filterStrategy: "전략",
  filterSort: "정렬",
  filterLimit: "최대 개수",
  filterKrwOnly: "KRW 마켓만",
  filterExcludeWarnings: "warning 제외",
  marketKr: "국내",
  marketUs: "해외",
  marketCrypto: "암호화폐",
  strategyAny: "기본",
  strategyOversold: "과매도 (RSI ≤ 30)",
  strategyMomentum: "모멘텀",
  strategyHighVolume: "거래량 상위",
  sortAuto: "자동",
  sortVolume: "거래량",
  sortTradeAmount: "거래대금",
  sortChangeRate: "등락률",
  sortRsi: "RSI",
  runScreen: "후보 스캔",
  startResearch: "Research Session 시작",
  researchStarted: "Research Session 생성됨 (#",
  researchFailed: "Research Session 생성 실패",
  linkOrderPreview: "주문 Preview",
  colSymbol: "종목",
  colPrice: "가격",
  colChange: "등락률",
  colVolume: "거래량",
  colTradeAmount: "거래대금",
  colVolumeRatio: "거래량 배율",
  colRsi: "RSI",
  colMarketCap: "시총",
  colHeld: "보유",
  colWarnings: "데이터 경고",
  held: "보유 중",
  notHeld: "—",
  empty: "조건에 맞는 후보가 없습니다.",
  loadError: "후보 데이터를 불러오지 못했습니다.",
  rsiEnrichmentNote: "RSI enrichment ",
  warningsHeader: "Screening 경고",
};

export const tradeJournal = {
  pageTitle: "Position Thesis Journal",
  pageSubtitle: "보유 종목별 투자 가설과 전략을 기록하고 관리합니다.",
  filterMarket: "시장",
  marketAll: "전체",
  marketKR: "국내",
  marketUS: "해외",
  marketCRYPTO: "암호화폐",
  colSymbol: "종목",
  colWeight: "비중",
  colStatus: "상태",
  colThesis: "투자 가설",
  colTarget: "목표가",
  colStop: "손절가",
  colMinHold: "최소 보유",
  colResearch: "Research",
  colActions: "액션",
  statusPresent: "작성됨",
  statusMissing: "미작성",
  statusStale: "오래됨",
  actionEdit: "수정",
  actionCreate: "작성",
  actionResearch: "Research",
  conflictWarning: "Research 의견(SELL)과 Thesis가 충돌합니다.",
  empty: "보유 종목이 없습니다.",
  loadError: "데이터를 불러오지 못했습니다.",
  saveSuccess: "저장되었습니다.",
  saveError: "저장 실패",
  modalTitleEdit: "Thesis 수정",
  modalTitleCreate: "Thesis 작성",
  labelThesis: "투자 가설 (필수)",
  labelStrategy: "매매 전략",
  labelTargetPrice: "목표가",
  labelStopLoss: "손절가",
  labelMinHoldDays: "최소 보유 일수",
  labelStatus: "상태",
  labelNotes: "추가 메모",
  btnSave: "저장",
  placeholderThesis: "이 종목을 보유하는 핵심 이유를 입력하세요...",
};

export const retrospective = {
  pageTitle: "Research Retrospective",
  pageSubtitle: "Research Pipeline 분석/요약/사용자 결정/주문 결과 회고",
  filterDays: "기간(일)",
  filterMarket: "시장",
  filterAll: "전체",
  marketKR: "국내",
  marketUS: "해외",
  marketCRYPTO: "암호화폐",
  loadError: "Retrospective 데이터를 불러오지 못했습니다.",
  empty: "조건에 해당하는 데이터가 없습니다.",
  warningEmpty: "선택한 기간에 Research Summary가 없습니다.",
  cards: {
    sessions: "Sessions",
    summaries: "Summaries",
    realizedPnl: "실현 PnL 평균",
    unrealizedPnl: "미실현 PnL 평균",
  },
  distribution: {
    title: "AI vs User Decision",
    aiBuy: "AI buy",
    aiHold: "AI hold",
    aiSell: "AI sell",
    userAccept: "User accept",
    userReject: "User reject",
    userModify: "User modify",
    userDefer: "User defer",
    userPending: "User pending",
  },
  stageCoverage: {
    title: "Stage Coverage",
    stage: "Stage",
    coverage: "Coverage %",
    stale: "Stale %",
    unavailable: "Unavailable %",
  },
  stagePerformance: {
    title: "Stage 조합별 성과",
    combo: "조합",
    sample: "표본",
    winRate: "승률 %",
    avgPnl: "평균 PnL %",
  },
  decisions: {
    title: "Decision drill-down",
    symbol: "종목",
    market: "시장",
    decidedAt: "결정 시각",
    ai: "AI",
    user: "사용자",
    realized: "실현 PnL %",
    open: "Session 열기",
  },
};

export const journalRetrospective = {
  pageTitle: "Trade Journal History",
  pageSubtitle: "종료된 trade journal 항목 (closed / stopped / expired).",
  colSymbol: "종목",
  colThesis: "투자 가설",
  colSide: "방향",
  colPnL: "수익률",
  colStatus: "결과",
  colDate: "종료일",
  statusClosed: "익절/종료",
  statusStopped: "손절",
  statusExpired: "만기",
  empty: "복기할 데이터가 없습니다.",
  loadError: "데이터를 불러오지 못했습니다.",
};
