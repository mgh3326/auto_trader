export type InvestorFlowDataState = "empty" | "missing" | "stale" | "fresh" | "partial";

export interface InvestorFlowItem {
  symbol: string;
  market: "kr";
  dataState: "missing" | "stale" | "fresh";
  snapshotDate?: string | null;
  collectedAt?: string | null;
  source?: string | null;
  foreignNet?: number | null;
  institutionNet?: number | null;
  individualNet?: number | null;
  foreignNetBuyRank?: number | null;
  foreignNetSellRank?: number | null;
  institutionNetBuyRank?: number | null;
  institutionNetSellRank?: number | null;
  doubleBuy: boolean;
  doubleSell: boolean;
  foreignConsecutiveBuyDays?: number | null;
  foreignConsecutiveSellDays?: number | null;
  institutionConsecutiveBuyDays?: number | null;
  institutionConsecutiveSellDays?: number | null;
  individualConsecutiveBuyDays?: number | null;
  individualConsecutiveSellDays?: number | null;
}

export interface InvestorFlowResponse {
  market: "kr";
  asOf: string;
  source?: string | null;
  dataState: InvestorFlowDataState;
  items: InvestorFlowItem[];
}
