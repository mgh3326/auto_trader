export type SignalTab = "mine" | "kr" | "us" | "crypto";
export type SignalRelation = "held" | "watchlist" | "both" | "none";

export interface SignalRelatedSymbol {
  symbol: string;
  market: "kr" | "us" | "crypto";
  displayName: string;
}

export interface SignalCard {
  id: string;
  source: "analysis" | "issue" | "brief";
  title: string;
  market: "kr" | "us" | "crypto";
  decisionLabel?: "buy" | "hold" | "sell" | "watch" | "neutral" | null;
  confidence?: number | null;
  severity?: "low" | "medium" | "high" | null;
  summary?: string | null;
  generatedAt: string;
  relatedSymbols: SignalRelatedSymbol[];
  relatedIssueIds: string[];
  supportingNewsIds: number[];
  rationale?: string | null;
  relation: SignalRelation;
}

export interface SignalsMeta {
  emptyReason?: string | null;
  warnings: string[];
}

export interface SignalsResponse {
  tab: SignalTab;
  asOf: string;
  items: SignalCard[];
  meta: SignalsMeta;
}
