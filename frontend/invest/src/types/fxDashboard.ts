export type FxDashboardDataState = "fresh" | "partial" | "missing" | "stale" | "error";
export type FxDashboardTone = "up" | "down" | "flat" | "unknown";
export type DefenseSignalState = "none" | "watch" | "elevated" | "after_verification_required";
export type DefenseSignalConfidence = "low" | "medium" | "high";
export type FxDisclaimerSeverity = "info" | "caution" | "warning";
export type FxDashboardThresholdState = "watch" | "near" | "breached";

export type FxDashboardSourceFreshness = {
  source: string;
  label: string;
  dataState: FxDashboardDataState;
  updatedAt?: string | null;
  staleAfterMinutes?: number | null;
  warning?: string | null;
};

export type FxDashboardDisclaimer = {
  code: string;
  severity: FxDisclaimerSeverity;
  textKo: string;
};

export type FxDashboardQuoteMetric = {
  symbol: string;
  label?: string | null;
  value?: number | null;
  spot?: number | null;
  change?: number | null;
  changePct?: number | null;
  tone: FxDashboardTone;
  updatedAt?: string | null;
  dataState?: FxDashboardDataState | null;
  source: string;
};

export type FxDashboardThreshold = {
  level: number;
  label: string;
  distancePct: number;
  state: FxDashboardThresholdState;
};

export type FxDashboardEvidenceItem = {
  kind: string;
  labelKo: string;
  value?: string | null;
  source: string;
  dataState: FxDashboardDataState;
};

export type FxDashboardDefenseSignal = {
  state: DefenseSignalState;
  score: number;
  confidence: DefenseSignalConfidence;
  labelKo: string;
  summaryKo: string;
  reasonsKo: string[];
  evidence: FxDashboardEvidenceItem[];
  notConfirmedIntervention: boolean;
  needsAfterVerification: boolean;
};

export type FxDashboardCollectionItem = {
  symbol: string;
  label: string;
  value?: number | null;
  changePct?: number | null;
  dataState: FxDashboardDataState;
  source: string;
};

export type FxDashboardForeignFlowItem = {
  label: string;
  value?: string | null;
  source: string;
  dataState: FxDashboardDataState;
};

export type FxDashboardForeignFlowSection = {
  dataState: FxDashboardDataState;
  summaryKo: string;
  items: FxDashboardForeignFlowItem[];
};

export type FxDashboardNewsItem = {
  title: string;
  source: string;
  publishedAt?: string | null;
  url?: string | null;
  dataState: FxDashboardDataState;
};

export type FxDashboardNewsSection = {
  dataState: FxDashboardDataState;
  items: FxDashboardNewsItem[];
  warning?: string | null;
};

export type FxDashboardEventItem = {
  title: string;
  startsAt?: string | null;
  source: string;
  dataState: FxDashboardDataState;
};

export type FxDashboardEventsSection = {
  dataState: FxDashboardDataState;
  items: FxDashboardEventItem[];
  warning?: string | null;
};

export type FxDashboardAfterVerification = {
  dataState: FxDashboardDataState;
  officialEvidence: FxDashboardEvidenceItem[];
  dealerEvidence: FxDashboardEvidenceItem[];
  ndfEvidence: FxDashboardEvidenceItem[];
  summaryKo: string;
};

export type FxDashboardResponse = {
  asOf: string;
  dataState: FxDashboardDataState;
  warnings: string[];
  disclaimers: FxDashboardDisclaimer[];
  sourceFreshness: FxDashboardSourceFreshness[];
  usdKrw: FxDashboardQuoteMetric;
  thresholds: FxDashboardThreshold[];
  defenseSignal: FxDashboardDefenseSignal;
  globalDollar: FxDashboardCollectionItem[];
  krwCrosses: FxDashboardCollectionItem[];
  foreignFlow: FxDashboardForeignFlowSection;
  news: FxDashboardNewsSection;
  events: FxDashboardEventsSection;
  afterVerification: FxDashboardAfterVerification;
};
