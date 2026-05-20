// ROB-265 Plan 5 — API client for /invest/reports.
//
// Calls the Plan 3 backend endpoints
// ``GET /invest/api/investment-reports`` and
// ``GET /invest/api/investment-reports/{report_uuid}`` and converts the
// snake_case JSON to camelCase TypeScript shapes from
// ``../types/investmentReports``.

import type {
  InvestmentReport,
  InvestmentReportBundle,
  InvestmentReportItem,
  InvestmentReportItemDecision,
  InvestmentReportListResponse,
  InvestmentWatchAlert,
  InvestmentWatchEvent,
  Market,
  MarketSession,
  AccountScope,
  ReportStatus,
  SnapshotFreshnessSummary,
  ReportSnapshotBundle,
  ReportSnapshotBundleItem,
  ReportSnapshotBundleSummary,
  ReportSnapshotDetail,
  BundleItemRole,
  BundleStatus,
  SnapshotFreshnessStatus,
  SnapshotKind,
  SnapshotSourceKind,
} from "../types/investmentReports";

const LIST_ENDPOINT = "/invest/api/investment-reports";
const BUNDLE_ENDPOINT = (uuid: string) =>
  `/invest/api/investment-reports/${encodeURIComponent(uuid)}`;
const UNAVAILABLE_LABEL = "확인 불가";

async function readJson<T>(endpoint: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(endpoint, { credentials: "include", signal });
  if (!res.ok) {
    throw new Error(`${endpoint} ${res.status}`);
  }
  return res.json();
}

type ApiReport = Record<string, unknown>;
type ApiItem = Record<string, unknown>;
type ApiDecision = Record<string, unknown>;
type ApiAlert = Record<string, unknown>;
type ApiEvent = Record<string, unknown>;

function asString(value: unknown, fallback: string = UNAVAILABLE_LABEL): string {
  return typeof value === "string" ? value : fallback;
}

function asOptionalString(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  return typeof value === "string" ? value : null;
}

function asNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function asArray<T = unknown>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function normalizeReport(raw: ApiReport): InvestmentReport {
  return {
    reportUuid: asString(raw.report_uuid),
    reportType: asString(raw.report_type),
    market: asString(raw.market, "kr") as Market,
    marketSession: asOptionalString(raw.market_session) as MarketSession | null,
    accountScope: asOptionalString(raw.account_scope) as AccountScope | null,
    executionMode: asString(raw.execution_mode, "advisory_only") as
      | "advisory_only"
      | "mock_preview",
    createdByProfile: asString(raw.created_by_profile),
    title: asString(raw.title),
    summary: asString(raw.summary),
    riskSummary: asOptionalString(raw.risk_summary),
    thesisText: asOptionalString(raw.thesis_text),
    noActionNote: asOptionalString(raw.no_action_note),
    marketSnapshot: asRecord(raw.market_snapshot),
    portfolioSnapshot: asRecord(raw.portfolio_snapshot),
    previousReportUuid: asOptionalString(raw.previous_report_uuid),
    status: asString(raw.status, "draft") as ReportStatus,
    metadata: asRecord(raw.metadata),
    createdAt: asString(raw.created_at),
    updatedAt: asString(raw.updated_at),
    publishedAt: asOptionalString(raw.published_at),
    validUntil: asOptionalString(raw.valid_until),
    // ROB-269 Phase 3 — snapshot metadata. Backend serialises explicit
    // ``null`` for legacy reports; we round-trip that as ``null`` here so
    // the UI can distinguish "report has no bundle" (legacy) from "bundle
    // has incomplete data" (per-kind freshness on the summary).
    snapshotBundleUuid: asOptionalString(raw.snapshot_bundle_uuid),
    snapshotPolicyVersion: asOptionalString(raw.snapshot_policy_version),
    snapshotCoverageSummary: asOptionalRecord(raw.snapshot_coverage_summary),
    snapshotFreshnessSummary: raw.snapshot_freshness_summary == null
      ? null
      : (asRecord(raw.snapshot_freshness_summary) as SnapshotFreshnessSummary),
    sourceConflicts: asOptionalRecord(raw.source_conflicts),
    unavailableSources: asOptionalRecord(raw.unavailable_sources),
  };
}

function asOptionalRecord(
  value: unknown,
): Record<string, unknown> | null {
  if (value === null || value === undefined) return null;
  return asRecord(value);
}

function normalizeItem(raw: ApiItem): InvestmentReportItem {
  return {
    itemUuid: asString(raw.item_uuid),
    itemKind: asString(raw.item_kind, "action") as "action" | "watch" | "risk",
    symbol: asOptionalString(raw.symbol),
    side: asOptionalString(raw.side) as "buy" | "sell" | null,
    intent: asString(raw.intent, "buy_review") as InvestmentReportItem["intent"],
    targetKind: asString(raw.target_kind, "asset") as InvestmentReportItem["targetKind"],
    priority: asNumber(raw.priority, 0),
    confidence: (raw.confidence as string | number | null | undefined) ?? null,
    rationale: asString(raw.rationale),
    evidenceSnapshot: asRecord(raw.evidence_snapshot),
    watchCondition:
      raw.watch_condition === null || raw.watch_condition === undefined
        ? null
        : asRecord(raw.watch_condition),
    triggerChecklist: asArray(raw.trigger_checklist),
    maxAction: asRecord(raw.max_action),
    validUntil: asOptionalString(raw.valid_until),
    status: asString(raw.status, "proposed") as InvestmentReportItem["status"],
    metadata: asRecord(raw.metadata),
    createdAt: asString(raw.created_at),
    updatedAt: asString(raw.updated_at),
  };
}

function normalizeDecision(raw: ApiDecision): InvestmentReportItemDecision {
  return {
    decisionUuid: asString(raw.decision_uuid),
    decision: asString(raw.decision, "approve") as InvestmentReportItemDecision["decision"],
    actor: asString(raw.actor),
    decisionNote: asOptionalString(raw.decision_note),
    approvedPayloadSnapshot:
      raw.approved_payload_snapshot === null ||
      raw.approved_payload_snapshot === undefined
        ? null
        : asRecord(raw.approved_payload_snapshot),
    createdAt: asString(raw.created_at),
  };
}

function normalizeAlert(raw: ApiAlert): InvestmentWatchAlert {
  return {
    alertUuid: asString(raw.alert_uuid),
    sourceReportUuid: asString(raw.source_report_uuid),
    sourceItemUuid: asString(raw.source_item_uuid),
    market: asString(raw.market, "kr") as Market,
    targetKind: asString(raw.target_kind, "asset") as InvestmentWatchAlert["targetKind"],
    symbol: asString(raw.symbol),
    metric: asString(raw.metric, "price") as InvestmentWatchAlert["metric"],
    operator: asString(raw.operator, "below") as InvestmentWatchAlert["operator"],
    threshold: asString(raw.threshold, "0"),
    thresholdKey: asString(raw.threshold_key),
    intent: asString(raw.intent, "buy_review") as InvestmentWatchAlert["intent"],
    actionMode: asString(raw.action_mode, "notify_only") as InvestmentWatchAlert["actionMode"],
    rationale: asString(raw.rationale),
    triggerChecklist: asArray(raw.trigger_checklist),
    maxAction: asRecord(raw.max_action),
    validUntil: asString(raw.valid_until),
    status: asString(raw.status, "active") as InvestmentWatchAlert["status"],
    metadata: asRecord(raw.metadata),
    createdAt: asString(raw.created_at),
    activatedAt: asString(raw.activated_at),
    updatedAt: asString(raw.updated_at),
  };
}

function normalizeEvent(raw: ApiEvent): InvestmentWatchEvent {
  return {
    eventUuid: asString(raw.event_uuid),
    alertId: (raw.alert_id as number | null | undefined) ?? null,
    sourceReportUuid: asString(raw.source_report_uuid),
    sourceItemUuid: asString(raw.source_item_uuid),
    market: asString(raw.market, "kr") as Market,
    targetKind: asString(raw.target_kind, "asset") as InvestmentWatchEvent["targetKind"],
    symbol: asString(raw.symbol),
    metric: asString(raw.metric, "price") as InvestmentWatchEvent["metric"],
    operator: asString(raw.operator, "below") as InvestmentWatchEvent["operator"],
    threshold: asString(raw.threshold, "0"),
    thresholdKey: asString(raw.threshold_key),
    intent: asString(raw.intent, "buy_review") as InvestmentWatchEvent["intent"],
    actionMode: asString(raw.action_mode, "notify_only") as InvestmentWatchEvent["actionMode"],
    currentValue: asOptionalString(raw.current_value),
    scannerSnapshot: asRecord(raw.scanner_snapshot),
    outcome: asString(raw.outcome, "notified") as InvestmentWatchEvent["outcome"],
    followUpReportItemId:
      (raw.follow_up_report_item_id as number | null | undefined) ?? null,
    correlationId: asString(raw.correlation_id),
    kstDate: asString(raw.kst_date),
    deliveryStatus: asString(
      raw.delivery_status,
      "pending",
    ) as InvestmentWatchEvent["deliveryStatus"],
    deliveryReason: asOptionalString(raw.delivery_reason),
    deliveredAt: asOptionalString(raw.delivered_at),
    deliveryAttempts: asNumber(raw.delivery_attempts, 0),
    createdAt: asString(raw.created_at),
  };
}

export async function fetchInvestmentReports(
  params: {
    market?: Market;
    marketSession?: MarketSession;
    accountScope?: AccountScope;
    status?: ReportStatus;
    reportType?: string;
    limit?: number;
  } = {},
  signal?: AbortSignal,
): Promise<InvestmentReportListResponse> {
  const search = new URLSearchParams();
  if (params.market) search.set("market", params.market);
  if (params.marketSession) search.set("market_session", params.marketSession);
  if (params.accountScope) search.set("account_scope", params.accountScope);
  if (params.status) search.set("status", params.status);
  if (params.reportType) search.set("report_type", params.reportType);
  if (params.limit) search.set("limit", String(params.limit));
  const qs = search.toString();
  const url = qs ? `${LIST_ENDPOINT}?${qs}` : LIST_ENDPOINT;
  const raw = await readJson<{ reports?: ApiReport[] }>(url, signal);
  return { reports: asArray<ApiReport>(raw.reports).map(normalizeReport) };
}

export async function fetchInvestmentReportBundle(
  reportUuid: string,
  signal?: AbortSignal,
): Promise<InvestmentReportBundle> {
  const raw = await readJson<{
    report?: ApiReport;
    items?: ApiItem[];
    decisions_by_item_uuid?: Record<string, ApiDecision[]>;
    alerts?: ApiAlert[];
    events?: ApiEvent[];
  }>(BUNDLE_ENDPOINT(reportUuid), signal);

  const decisionsRaw = raw.decisions_by_item_uuid ?? {};
  const decisionsByItemUuid: Record<string, InvestmentReportItemDecision[]> = {};
  for (const [itemUuid, decisions] of Object.entries(decisionsRaw)) {
    decisionsByItemUuid[itemUuid] = asArray<ApiDecision>(decisions).map(
      normalizeDecision,
    );
  }

  return {
    report: normalizeReport(asRecord(raw.report)),
    items: asArray<ApiItem>(raw.items).map(normalizeItem),
    decisionsByItemUuid,
    alerts: asArray<ApiAlert>(raw.alerts).map(normalizeAlert),
    events: asArray<ApiEvent>(raw.events).map(normalizeEvent),
  };
}

export { UNAVAILABLE_LABEL };

// ROB-275 — Snapshot evidence viewer API client.

const BUNDLE_SNAPSHOT_ENDPOINT = (reportUuid: string) =>
  `/invest/api/investment-reports/${encodeURIComponent(reportUuid)}/snapshot-bundle`;
const SNAPSHOT_DETAIL_ENDPOINT = (
  reportUuid: string,
  snapshotUuid: string,
) =>
  `/invest/api/investment-reports/${encodeURIComponent(reportUuid)}/snapshots/${encodeURIComponent(snapshotUuid)}`;

type ApiBundle = Record<string, unknown>;
type ApiBundleItem = Record<string, unknown>;
type ApiSnapshotDetail = Record<string, unknown>;

function normalizeBundleSummary(
  raw: ApiBundle,
): ReportSnapshotBundleSummary {
  return {
    bundleUuid: asString(raw.bundle_uuid),
    purpose: asString(raw.purpose),
    market: asString(raw.market, "kr") as ReportSnapshotBundleSummary["market"],
    accountScope: asOptionalString(
      raw.account_scope,
    ) as ReportSnapshotBundleSummary["accountScope"],
    policyVersion: asString(raw.policy_version),
    status: asString(raw.status, "complete") as BundleStatus,
    asOf: asString(raw.as_of),
    coverageSummary: asRecord(raw.coverage_summary),
    freshnessSummary: asRecord(raw.freshness_summary),
    createdAt: asString(raw.created_at),
  };
}

function normalizeBundleItem(
  raw: ApiBundleItem,
): ReportSnapshotBundleItem {
  return {
    snapshotUuid: asString(raw.snapshot_uuid),
    role: asString(raw.role, "required") as BundleItemRole,
    snapshotKind: asString(raw.snapshot_kind, "portfolio") as SnapshotKind,
    sourceKind: asString(raw.source_kind, "manual") as SnapshotSourceKind,
    market: asString(raw.market, "kr") as ReportSnapshotBundleItem["market"],
    symbol: asOptionalString(raw.symbol),
    accountScope: asOptionalString(
      raw.account_scope,
    ) as ReportSnapshotBundleItem["accountScope"],
    freshnessStatus: asString(
      raw.freshness_status,
      "fresh",
    ) as SnapshotFreshnessStatus,
    asOf: asString(raw.as_of),
    validUntil: asOptionalString(raw.valid_until),
    sourceTable: asOptionalString(raw.source_table),
    sourceId:
      typeof raw.source_id === "number" && Number.isFinite(raw.source_id)
        ? (raw.source_id as number)
        : null,
    sourceUri: asOptionalString(raw.source_uri),
    payloadSizeBytes:
      typeof raw.payload_size_bytes === "number" &&
      Number.isFinite(raw.payload_size_bytes)
        ? (raw.payload_size_bytes as number)
        : null,
  };
}

function normalizeSnapshotDetail(
  raw: ApiSnapshotDetail,
): ReportSnapshotDetail {
  return {
    snapshotUuid: asString(raw.snapshot_uuid),
    role: asString(raw.role, "required") as BundleItemRole,
    snapshotKind: asString(raw.snapshot_kind, "portfolio") as SnapshotKind,
    sourceKind: asString(raw.source_kind, "manual") as SnapshotSourceKind,
    market: asString(raw.market, "kr") as ReportSnapshotDetail["market"],
    symbol: asOptionalString(raw.symbol),
    accountScope: asOptionalString(
      raw.account_scope,
    ) as ReportSnapshotDetail["accountScope"],
    sourceTable: asOptionalString(raw.source_table),
    sourceId:
      typeof raw.source_id === "number" && Number.isFinite(raw.source_id)
        ? (raw.source_id as number)
        : null,
    sourceUri: asOptionalString(raw.source_uri),
    freshnessStatus: asString(
      raw.freshness_status,
      "fresh",
    ) as SnapshotFreshnessStatus,
    asOf: asString(raw.as_of),
    validUntil: asOptionalString(raw.valid_until),
    sourceTimestampsJson: asRecord(raw.source_timestamps_json),
    coverageJson: asRecord(raw.coverage_json),
    errorsJson: asRecord(raw.errors_json),
    payloadJson: asRecord(raw.payload_json),
  };
}

export async function fetchReportSnapshotBundle(
  reportUuid: string,
  signal?: AbortSignal,
): Promise<ReportSnapshotBundle> {
  const raw = await readJson<{
    bundle?: ApiBundle | null;
    items?: ApiBundleItem[];
    unavailable_sources?: Record<string, unknown> | null;
    source_conflicts?: Record<string, unknown> | null;
    legacy_no_snapshot?: boolean;
  }>(BUNDLE_SNAPSHOT_ENDPOINT(reportUuid), signal);

  return {
    bundle:
      raw.bundle == null
        ? null
        : normalizeBundleSummary(raw.bundle as ApiBundle),
    items: asArray<ApiBundleItem>(raw.items).map(normalizeBundleItem),
    unavailableSources: asOptionalRecord(raw.unavailable_sources),
    sourceConflicts: asOptionalRecord(raw.source_conflicts),
    legacyNoSnapshot: Boolean(raw.legacy_no_snapshot),
  };
}

export async function fetchReportSnapshotDetail(
  reportUuid: string,
  snapshotUuid: string,
  signal?: AbortSignal,
): Promise<ReportSnapshotDetail> {
  const raw = await readJson<ApiSnapshotDetail>(
    SNAPSHOT_DETAIL_ENDPOINT(reportUuid, snapshotUuid),
    signal,
  );
  return normalizeSnapshotDetail(raw);
}
