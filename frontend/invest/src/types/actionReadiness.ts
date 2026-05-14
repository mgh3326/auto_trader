import type { CoverageActionability, CoverageState, InvestCoverageCounts } from "./coverage";

export type ActionReadinessState =
  | "ready"
  | "degraded"
  | "blocked"
  | "missing"
  | "unsupported"
  | "unknown";

export type ActionReadinessAuthority =
  | "kis_live_broker"
  | "auto_trader_read_model"
  | "manual_or_paper_reference"
  | "external_reference"
  | "unsupported";

export type ActionReportImpact =
  | "none"
  | "degrades_report"
  | "blocks_buy_report"
  | "blocks_sell_report"
  | "blocks_all_action_reports";

export interface ActionReadinessLink {
  label: string;
  href: string;
}

export interface ActionReadinessFamily {
  key: string;
  labelKo: string;
  category: string;
  state: ActionReadinessState;
  impact: ActionReportImpact;
  authority: ActionReadinessAuthority;
  sourceOfTruth: string;
  references: string[];
  latestAt: string | null;
  latestDate: string | null;
  counts: InvestCoverageCounts | null;
  coverageState: CoverageState | null;
  actionability: CoverageActionability;
  blockers: string[];
  warnings: string[];
  notes: string[];
  links: ActionReadinessLink[];
}

export interface KrActionReadinessResponse {
  market: "kr";
  asOf: string;
  symbol: string | null;
  overallState: ActionReadinessState;
  canGenerateBuyReport: boolean;
  canGenerateSellReport: boolean;
  families: ActionReadinessFamily[];
  blockers: string[];
  degradedSignals: string[];
  sourcePolicy: string[];
  notes: string[];
}
