export type CalendarTab = "all" | "economic" | "earnings" | "disclosure" | "crypto";
export type CalendarMarket = "kr" | "us" | "crypto" | "global";
export type EventType = "earnings" | "economic" | "disclosure" | "crypto" | "other";
export type CalendarRelation = "held" | "watchlist" | "both" | "none";
export type CalendarSourceState = "fresh" | "stale" | "failed" | "missing";
export type CalendarDayState =
  | "loaded"
  | "empty"
  | "partial"
  | "missing"
  | "error"
  | "stale";

export interface CalendarSourceStatus {
  source: string;
  category: string;
  market: string;
  state: CalendarSourceState;
  lastSuccessAt?: string | null;
  lastFailureAt?: string | null;
  lastError?: string | null;
  succeededPartitions: number;
  failedPartitions: number;
  missingPartitions: number;
  eventCount: number;
}

export interface CalendarCoverage {
  fromDate: string;
  toDate: string;
  expectedPartitions: number;
  succeededPartitions: number;
  failedPartitions: number;
  missingPartitions: number;
  totalEvents: number;
}

export interface CalendarRelatedSymbol {
  symbol: string;
  market: "kr" | "us" | "crypto";
  displayName: string;
}

export interface CalendarEvent {
  eventId: string;
  title: string;
  market: CalendarMarket;
  eventType: EventType;
  eventTimeLocal?: string | null;
  source: string;
  actual?: string | null;
  forecast?: string | null;
  previous?: string | null;
  relatedSymbols: CalendarRelatedSymbol[];
  relation: CalendarRelation;
  badges: ("holdings" | "watchlist" | "major")[];
}

export interface CalendarCluster {
  clusterId: string;
  label: string;
  eventType: EventType;
  market: CalendarMarket;
  eventCount: number;
  topEvents: CalendarEvent[];
}

export interface CalendarDay {
  date: string;
  events: CalendarEvent[];
  clusters: CalendarCluster[];
  dataState: CalendarDayState;
}

export interface CalendarResponse {
  tab: CalendarTab;
  fromDate: string;
  toDate: string;
  asOf: string;
  days: CalendarDay[];
  meta: {
    warnings: string[];
    sourceFreshness: CalendarSourceStatus[];
    coverage: CalendarCoverage | null;
  };
}

export interface WeeklySection {
  date: string;
  reportType: string;
  market?: string | null;
  title: string;
  body: string;
}

export interface WeeklySummaryResponse {
  weekStart: string;
  asOf: string;
  sections: WeeklySection[];
  partial: boolean;
  missingDates: string[];
}
