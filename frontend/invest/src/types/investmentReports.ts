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
  // ROB-318 Phase 3 — deterministic report-level diagnostics. Null on legacy
  // reports. Internal keys stay snake_case (the API passes the JSON through
  // without deep-transforming nested objects, same as snapshotFreshnessSummary).
  snapshotReportDiagnostics?: SnapshotReportDiagnostics | null;
}

// ROB-318 Phase 3 — typed shape of ``snapshot_report_diagnostics``. Mirrors the
// backend ``app/services/action_report/common/diagnostics.py`` builders. Keys
// are snake_case to match the wire format (no deep camelCase transform).
export type WhyNoActionKind =
  | "data_insufficient"
  | "stale_gated"
  | "real_no_action";

export interface WhyNoAction {
  kind: WhyNoActionKind;
  blocking_sources: string[];
  reason_ko: string;
}

export type ReportQualityGrade =
  | "high_confidence"
  | "informational_only"
  | "no_action";

export interface ReportQualitySummary {
  grade: ReportQualityGrade;
  bundle_status?: string | null;
  freshness_overall?: SnapshotFreshnessStatus | string | null;
  kind_status_counts?: Record<string, number>;
  fresh_coverage_pct?: number;
  // ROB-323 — core vs optional vs external split.
  core_fresh_coverage_pct?: number;
  optional_fresh_coverage_pct?: number;
  external_cross_check_status?: SnapshotFreshnessStatus | string | null;
}

// ROB-323 — external cross-check / data-quality audit (embedded in
// snapshot_report_diagnostics). External probes never affect report generation.
export interface ExternalCrossCheck {
  status?: SnapshotFreshnessStatus | string | null;
  reason_code?: string | null;
  reason?: string | null;
  as_of?: string | null;
  affects_report_generation: false;
}

export interface DataQualityGap {
  severity: "info" | "warning" | "blocking";
  kind: string;
  sources?: string[];
  message: string;
}

export interface DataQualityAudit {
  snapshot_bundle_uuid?: string | null;
  core: {
    status: "usable" | "degraded";
    blocking_gaps: string[];
    fresh_coverage_pct?: number;
  };
  external_cross_checks: Record<string, ExternalCrossCheck>;
  gaps: DataQualityGap[];
}

export interface DataSufficiencySource {
  status?: SnapshotFreshnessStatus | string | null;
  reason_code?: string | null;
  reason?: string | null;
  as_of?: string | null;
}

export interface SnapshotReportDiagnostics {
  why_no_action?: WhyNoAction | null;
  data_sufficiency_by_source?: Record<string, DataSufficiencySource>;
  report_quality_summary?: ReportQualitySummary | null;
  data_quality_audit?: DataQualityAudit | null;
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

// ROB-274 — proposal-state vocabulary.
export type ProposalOperation =
  | "create"
  | "modify"
  | "cancel"
  | "keep"
  | "replace"
  | "review";

export interface ProposalTargetRef {
  type: "investment_watch_alert" | "broker_order" | "ambiguous";
  id?: string | null;
  status?: string | null;
  broker?: string | null;
  raw?: Record<string, unknown> | null;
  candidates?: Array<Record<string, unknown>> | null;
}

export interface ProposalDiffEntry {
  field: string;
  from: unknown;
  to: unknown;
}

// ROB-554 — a live order linked to a report item via report_item_uuid (ROB-473),
// with the reconcile-written fill rollup. Read-only; surfaced on the decision log.
export interface LinkedOrder {
  broker?: string | null;
  accountScope?: string | null;
  market?: string | null;
  orderNo?: string | null;
  ledgerId: number;
  symbol?: string | null;
  side?: string | null;
  status?: string | null;
  filledQty?: number | string | null;
  avgFillPrice?: number | string | null;
  orderTime?: string | null;
  reconciledAt?: string | null;
  exitReason?: string | null;
  thesis?: string | null;
  reportItemUuid?: string | null;
}

export interface ForecastLink {
  forecastId: string;
  status: string;
  outcome: boolean | null;
  reviewDate: string | null;
  direction: string | null;
  targetPrice: number | null;
  probability: number;
  brierScore: number | null;
  resolutionSource: string | null;
  correlationId?: string | null;
}

export interface RetrospectiveLink {
  retrospectiveId: number;
  outcome: string;
  lesson: string | null;
  resultSummary: string | null;
  rootCauseClass: string | null;
  triggerType: string | null;
  pnlPct: number | null;
  createdAt: string | null;
  correlationId?: string | null;
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
  // ROB-274 — proposal-state fields. Optional/nullable so legacy items
  // (pre-ROB-274) remain valid against this interface.
  operation?: ProposalOperation | null;
  targetRef?: ProposalTargetRef | null;
  currentState?: Record<string, unknown> | null;
  proposedState?: Record<string, unknown> | null;
  diff?: ProposalDiffEntry[] | null;
  applyPolicy?: "requires_user_approval" | null;
  // ROB-308 / ROB-322 — final-item classification + source citations.
  // Optional/nullable so legacy items remain valid.
  decisionBucket?: string | null;
  citedSymbolReportUuid?: string | null;
  citedDimensionReportUuids?: string[];
  // ROB-554 — live orders linked to this item (null when none).
  linkedOrders?: LinkedOrder[] | null;
  // ROB-715 — backend-derived summary of evidence_snapshot.structured_evidence.
  structuredEvidenceSummary?: string | null;
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

// ROB-322 — KR /invest/reports five-section review surface. A read-time
// view-layer projection over the locked decision_bucket vocab + report
// diagnostics; no new persisted classification.
export type ReviewSectionKey =
  | "new_buy_candidate"
  | "held_strategy_review"
  | "watch_only"
  | "excluded_or_unavailable";

// ``WhyNoActionKind`` is defined above (ROB-318 diagnostics) and reused here.

export interface ReviewSection {
  key: ReviewSectionKey;
  labelKo: string;
  items: InvestmentReportItem[];
}

export interface NoActionSummary {
  kind?: WhyNoActionKind | null;
  reasonKo?: string | null;
  blockingSources: string[];
  excludedCount: number;
}

export interface ReportReviewSections {
  sections: ReviewSection[];
  noActionSummary?: NoActionSummary | null;
}

// ROB-335 — intraday ActionPacket (mirrors backend ActionPacket schema).
export type ActionVerdict =
  | "buy_review"
  | "limit_wait"
  | "no_new_buy_candidates"
  | "sell_review"
  | "trim_review"
  | "add_review"
  | "keep"
  | "no_add"
  | "watch_only"
  | "rejected"
  | "data_gap";

export interface ActionPacketEntry {
  verdict: ActionVerdict;
  symbol?: string | null;
  side?: "buy" | "sell" | null;
  rationale: string;
  itemUuid?: string | null;
  priority?: number | null;
  rank?: number | null;
  rejectOrWaitReason?: string | null;
  evidenceSnapshot: Record<string, unknown>;
}

export interface DataGapEntry {
  source: string;
  status?: string | null;
  reason?: string | null;
}

export interface ActionPacket {
  heldActions: ActionPacketEntry[];
  newBuyCandidates: ActionPacketEntry[];
  noNewBuyReason?: string | null;
  riskReviews: ActionPacketEntry[];
  noActionReason?: NoActionSummary | null;
  dataGapsForNextCycle: DataGapEntry[];
}

export interface InvestmentReportBundle {
  report: InvestmentReport;
  items: InvestmentReportItem[];
  decisionsByItemUuid: Record<string, InvestmentReportItemDecision[]>;
  alerts: InvestmentWatchAlert[];
  events: InvestmentWatchEvent[];
  // ROB-322 — additive five-section projection. Null on legacy reports or
  // older backend; `items` remains the fallback rendering source.
  reviewSections?: ReportReviewSections | null;
  // ROB-335 — additive intraday ActionPacket projection. Null for legacy /
  // non-intraday reports.
  actionPacket?: ActionPacket | null;
  // ROB-715 — item→forecast/retrospective exact-join maps keyed by item UUID.
  forecastsByItemUuid?: Record<string, ForecastLink[]>;
  retrospectivesByItemUuid?: Record<string, RetrospectiveLink[]>;
}

export interface InvestmentReportListResponse {
  reports: InvestmentReport[];
}

// ROB-275 — Snapshot evidence viewer types. Mirrors
// ``app/schemas/investment_reports.py::ReportSnapshotBundle*`` and
// ``ReportSnapshotDetailResponse``. Snapshot literals duplicate the
// backend enums on purpose; if backend enums grow, update here too.

export type BundleStatus =
  | "complete"
  | "partial"
  | "stale_fallback"
  | "failed";

export type BundleItemRole =
  | "required"
  | "optional"
  | "fallback"
  | "conflict_evidence";

export type SnapshotKind =
  | "portfolio"
  | "market"
  | "news"
  | "symbol"
  | "candidate_universe"
  | "browser_probe"
  | "invest_page"
  | "journal"
  | "watch_context"
  | "naver_remote_debug"
  | "toss_remote_debug"
  | "llm_input_frozen";

// Mirrors backend SourceKind (app/schemas/investment_snapshots.py).
export type SnapshotSourceKind =
  | "kis_mcp"
  | "auto_trader_mcp"
  | "invest_api"
  | "naver_remote_debug"
  | "toss_remote_debug"
  | "combined"
  | "news_ingestor"
  | "manual"
  | "domain_ref";

export interface ReportSnapshotBundleSummary {
  bundleUuid: string;
  purpose: string;
  market: Market;
  accountScope: AccountScope | null;
  policyVersion: string;
  status: BundleStatus;
  asOf: string;
  coverageSummary: Record<string, unknown>;
  freshnessSummary: Record<string, unknown>;
  createdAt: string;
}

export interface ReportSnapshotBundleItem {
  snapshotUuid: string;
  role: BundleItemRole;
  snapshotKind: SnapshotKind;
  sourceKind: SnapshotSourceKind;
  market: Market;
  symbol: string | null;
  accountScope: AccountScope | null;
  freshnessStatus: SnapshotFreshnessStatus;
  asOf: string;
  validUntil: string | null;
  sourceTable: string | null;
  sourceId: number | null;
  sourceUri: string | null;
  payloadSizeBytes: number | null;
}

export interface ReportSnapshotBundle {
  bundle: ReportSnapshotBundleSummary | null;
  items: ReportSnapshotBundleItem[];
  unavailableSources: Record<string, unknown> | null;
  sourceConflicts: Record<string, unknown> | null;
  legacyNoSnapshot: boolean;
}

export interface ReportSnapshotDetail {
  snapshotUuid: string;
  role: BundleItemRole;
  snapshotKind: SnapshotKind;
  sourceKind: SnapshotSourceKind;
  market: Market;
  symbol: string | null;
  accountScope: AccountScope | null;
  sourceTable: string | null;
  sourceId: number | null;
  sourceUri: string | null;
  freshnessStatus: SnapshotFreshnessStatus;
  asOf: string;
  validUntil: string | null;
  sourceTimestampsJson: Record<string, unknown>;
  coverageJson: Record<string, unknown>;
  errorsJson: Record<string, unknown>;
  payloadJson: Record<string, unknown>;
}

// ROB-279 Phase 5 — Stage artifact types. Mirrors the backend
// ``app/schemas/investment_stage_runs.py`` response shapes.

export type StageVerdict = "bull" | "bear" | "neutral" | "unavailable";

export type StageType =
  | "market"
  | "news"
  | "portfolio_journal"
  | "watch_context"
  | "candidate_universe"
  | "bull_reducer"
  | "bear_reducer"
  | "risk_review";

export interface StageArtifact {
  artifactUuid: string;
  runUuid: string;
  stageType: StageType;
  verdict: StageVerdict;
  confidence: number;
  summary: string | null;
  keyPoints: unknown[];
  buyEvidence: unknown[];
  sellEvidence: unknown[];
  riskEvidence: unknown[];
  missingData: unknown[];
  citedSnapshotUuids: string[];
  freshnessSummary: Record<string, unknown> | null;
  modelName: string | null;
  promptVersion: string | null;
  payloadHash: string | null;
  rawPayloadJson: Record<string, unknown> | null;
  createdAt: string;
}

export interface ReportStageArtifactsResponse {
  reportUuid: string;
  stageRunUuid: string | null;
  artifacts: StageArtifact[];
}
