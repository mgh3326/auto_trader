// ROB-315 Phase 3 — /invest/scalping (스캘핑 일지) types.
// Decimal-bearing fields arrive as strings (or null = "n/a") from the API so
// precision is preserved; the UI renders them verbatim or as "n/a".

export type ScalpingProduct = "spot" | "usdm_futures";
export type ReviewDecision = "review" | "keep" | "adjust" | "pause" | "disable";
export type ReviewStatus = "draft" | "reviewed" | "locked";
export type ActionType =
  | "parameter_change"
  | "investigate"
  | "pause"
  | "resume"
  | "add_guard"
  | "data_quality"
  | "no_change";
export type ActionStatus = "open" | "applied" | "skipped" | "superseded";

export interface ScalpingReviewMetrics {
  tradeCount: number;
  winCount: number;
  lossCount: number;
  anomalyCount: number;
  grossPnlUsdt: string | null;
  netPnlUsdt: string | null;
  netReturnBps: string | null;
  avgSlippageBps: string | null;
  avgSpreadBps: string | null;
  avgMaeBps: string | null;
  avgMfeBps: string | null;
  avgHoldingSeconds: number | null;
  exitReasonCounts: Record<string, number>;
}

export interface ScalpingReview {
  id: number;
  reviewDate: string;
  product: ScalpingProduct;
  accountScope: string;
  sessionTag: string;
  metrics: ScalpingReviewMetrics;
  observation: string | null;
  rootCause: string | null;
  improvement: string | null;
  nextRunPlan: string | null;
  decision: ReviewDecision;
  status: ReviewStatus;
  sourcePayload: Record<string, unknown> | null;
  createdAt: string | null;
  updatedAt: string | null;
}

export interface ScalpingReviewAction {
  id: number;
  reviewId: number;
  actionType: ActionType;
  title: string;
  rationale: string | null;
  targetComponent: string | null;
  proposedChange: string | null;
  expectedEffect: string | null;
  status: ActionStatus;
  createdAt: string | null;
  updatedAt: string | null;
}

export interface ScalpingTrade {
  id: number;
  openClientOrderId: string;
  symbol: string;
  side: string;
  qty: string | null;
  entryPrice: string | null;
  exitPrice: string | null;
  entrySlippageBps: string | null;
  exitSlippageBps: string | null;
  entrySpreadBps: string | null;
  exitSpreadBps: string | null;
  maeBps: string | null;
  mfeBps: string | null;
  netPnlUsdt: string | null;
  holdingSeconds: number | null;
  exitReason: string | null;
  isAnomaly: boolean;
}

export interface ScalpingReviewListResponse {
  items: ScalpingReview[];
}

export interface ScalpingReviewDetailResponse {
  review: ScalpingReview;
  actions: ScalpingReviewAction[];
}

export interface ScalpingTradesResponse {
  items: ScalpingTrade[];
}
