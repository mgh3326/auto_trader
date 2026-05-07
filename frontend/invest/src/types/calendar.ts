export type CalendarTab = "all" | "economic" | "earnings" | "disclosure" | "crypto";
export type CalendarMarket = "kr" | "us" | "crypto" | "global";
export type EventType = "earnings" | "economic" | "disclosure" | "crypto" | "other";
export type CalendarRelation = "held" | "watchlist" | "both" | "none";

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
}

export interface CalendarResponse {
  tab: CalendarTab;
  fromDate: string;
  toDate: string;
  asOf: string;
  days: CalendarDay[];
  meta: { warnings: string[] };
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
