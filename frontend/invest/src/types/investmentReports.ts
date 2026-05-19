// ROB-265 Plan 5 — TypeScript shapes for the /invest/reports frontend.
//
// Mirrors the backend response models in
// ``app/schemas/investment_reports.py``. The API client converts the
// snake_case JSON to these camelCase types.

export type InvestmentReportRequestState = "loading" | "ready" | "error";

export type Market = "kr" | "us" | "crypto";
export type MarketSession = "regular" | "nxt" | "pre" | "post" | "24x7";
export type AccountScope =
  | "kis_live"
  | "kis_mock"
  | "alpaca_paper"
  | "upbit_live";
export type ExecutionMode = "advisory_only" | "mock_preview";
export type ReportStatus =
  | "draft"
  | "published"
  | "decided"
  | "expired"
  | "superseded";

export type ItemKind = "action" | "watch" | "risk";
export type ItemSide = "buy" | "sell";
export type ItemIntent =
  | "buy_review"
  | "sell_review"
  | "risk_review"
  | "trend_recovery_review"
  | "rebalance_review";
export type TargetKind = "asset" | "index" | "fx";
export type ItemStatus =
  | "proposed"
  | "approved"
  | "denied"
  | "deferred"
  | "activated"
  | "expired";

export type WatchMetric = "price" | "rsi" | "trade_value";
export type WatchOperator = "above" | "below";
export type WatchActionMode = "notify_only" | "preview_only" | "approval_required";

export type DecisionVerb =
  | "approve"
  | "deny"
  | "defer"
  | "skip"
  | "partial_approve";

export type WatchAlertStatus = "active" | "triggered" | "expired" | "canceled";

export type WatchEventOutcome =
  | "notified"
  | "review_required"
  | "preview_attached"
  | "expired"
  | "ignored"
  | "failed";

export type DeliveryStatus = "pending" | "delivered" | "skipped" | "failed";

export interface InvestmentReport {
  reportUuid: string;
  reportType: string;
  market: Market;
  marketSession?: MarketSession | null;
  accountScope?: AccountScope | null;
  executionMode: ExecutionMode;
  createdByProfile: string;
  title: string;
  summary: string;
  riskSummary?: string | null;
  thesisText?: string | null;
  noActionNote?: string | null;
  marketSnapshot: Record<string, unknown>;
  portfolioSnapshot: Record<string, unknown>;
  previousReportUuid?: string | null;
  status: ReportStatus;
  metadata: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
  publishedAt?: string | null;
  validUntil?: string | null;
  // ROB-269 Phase 3 — bundle linkage + 3-layer stale gate inputs. All
  // optional; legacy reports (pre-Phase-3) serialise these as ``null``.
  snapshotBundleUuid?: string | null;
  snapshotPolicyVersion?: string | null;
  snapshotCoverageSummary?: Record<string, unknown> | null;
  snapshotFreshnessSummary?: SnapshotFreshnessSummary | null;
  sourceConflicts?: Record<string, unknown> | null;
  unavailableSources?: Record<string, unknown> | null;
}

// ROB-269 Phase 4 — typed shape of the snapshot freshness summary on the
// report response. ``overall`` is the bundle-level signal the Phase 3 DB
// CHECK consults; per-kind entries (portfolio / journal / watch_context /
// market / news / naver_remote_debug / toss_remote_debug / etc.) carry
// their own ``status`` and optional ``asOf``.
export type SnapshotFreshnessStatus =
  | "fresh"
  | "soft_stale"
  | "partial"
  | "hard_stale"
  | "failed"
  | "unavailable";

export interface SnapshotKindFreshness {
  status: SnapshotFreshnessStatus;
  asOf?: string | null;
  resultCount?: string | null;
}

export interface SnapshotFreshnessSummary {
  overall?: SnapshotFreshnessStatus | null;
  // Per-kind entries are keyed by snapshot_kind (e.g. ``portfolio``).
  [snapshotKind: string]:
    | SnapshotKindFreshness
    | SnapshotFreshnessStatus
    | null
    | undefined;
}

export interface InvestmentReportItem {
  itemUuid: string;
  itemKind: ItemKind;
  symbol?: string | null;
  side?: ItemSide | null;
  intent: ItemIntent;
  targetKind: TargetKind;
  priority: number;
  confidence?: number | string | null;
  rationale: string;
  evidenceSnapshot: Record<string, unknown>;
  watchCondition?: Record<string, unknown> | null;
  triggerChecklist: unknown[];
  maxAction: Record<string, unknown>;
  validUntil?: string | null;
  status: ItemStatus;
  metadata: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
}

export interface InvestmentReportItemDecision {
  decisionUuid: string;
  decision: DecisionVerb;
  actor: string;
  decisionNote?: string | null;
  approvedPayloadSnapshot?: Record<string, unknown> | null;
  createdAt: string;
}

export interface InvestmentWatchAlert {
  alertUuid: string;
  sourceReportUuid: string;
  sourceItemUuid: string;
  market: Market;
  targetKind: TargetKind;
  symbol: string;
  metric: WatchMetric;
  operator: WatchOperator;
  threshold: string;
  thresholdKey: string;
  intent: ItemIntent;
  actionMode: WatchActionMode;
  rationale: string;
  triggerChecklist: unknown[];
  maxAction: Record<string, unknown>;
  validUntil: string;
  status: WatchAlertStatus;
  metadata: Record<string, unknown>;
  createdAt: string;
  activatedAt: string;
  updatedAt: string;
}

export interface InvestmentWatchEvent {
  eventUuid: string;
  alertId?: number | null;
  sourceReportUuid: string;
  sourceItemUuid: string;
  market: Market;
  targetKind: TargetKind;
  symbol: string;
  metric: WatchMetric;
  operator: WatchOperator;
  threshold: string;
  thresholdKey: string;
  intent: ItemIntent;
  actionMode: WatchActionMode;
  currentValue?: string | null;
  scannerSnapshot: Record<string, unknown>;
  outcome: WatchEventOutcome;
  followUpReportItemId?: number | null;
  correlationId: string;
  kstDate: string;
  // Plan 4 hardening — Hermes delivery tracking.
  deliveryStatus: DeliveryStatus;
  deliveryReason?: string | null;
  deliveredAt?: string | null;
  deliveryAttempts: number;
  createdAt: string;
}

export interface InvestmentReportBundle {
  report: InvestmentReport;
  items: InvestmentReportItem[];
  decisionsByItemUuid: Record<string, InvestmentReportItemDecision[]>;
  alerts: InvestmentWatchAlert[];
  events: InvestmentWatchEvent[];
}

export interface InvestmentReportListResponse {
  reports: InvestmentReport[];
}
