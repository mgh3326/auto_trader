export type DisparityState = "fresh" | "partial" | "stale" | "missing";
export type DisparityTone = "discount" | "premium" | "parity" | "unknown";

export type DisparitySource = {
  source: string;
  sourceOfTruth: string;
  asOf?: string | null;
  stale: boolean;
  freshnessSec?: number | null;
  warnings: string[];
};

export type DisparityPeriodWindow = {
  period: "1d" | "5d" | "20d" | "60d";
  sampleCount: number;
  meanDisparityPct?: number | null;
  minDisparityPct?: number | null;
  maxDisparityPct?: number | null;
  zScore?: number | null;
  dataState: DisparityState;
  emptyReason?: string | null;
};

export type CommonPreferredDisparityCard = {
  id: string;
  commonSymbol: string;
  commonName: string;
  preferredSymbol: string;
  preferredName: string;
  exchange?: string | null;
  commonPrice?: number | null;
  preferredPrice?: number | null;
  disparityPct?: number | null;
  preferredDiscountPct?: number | null;
  preferredPremiumPct?: number | null;
  zScore?: number | null;
  primaryWindow: "1d" | "5d" | "20d" | "60d";
  windows: DisparityPeriodWindow[];
  tone: DisparityTone;
  dataState: DisparityState;
  emptyReason?: string | null;
  formula: string;
  source: DisparitySource;
  warnings: string[];
  caution: string;
};

export type CommonPreferredDisparityResponse = {
  market: "kr";
  state: DisparityState;
  asOf: string;
  cards: CommonPreferredDisparityCard[];
  emptyReason?: string | null;
  warnings: string[];
  notes: string[];
};
