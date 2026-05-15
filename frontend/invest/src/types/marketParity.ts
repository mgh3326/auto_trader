export type MarketParityState = "fresh" | "partial" | "stale" | "missing" | "disabled";
export type MarketParityTone = "premium" | "discount" | "flat" | "unknown";
export type MarketParityCardType =
  | "index_implied_parity"
  | "stablecoin_fx_premium"
  | "crypto_kimchi_premium"
  | "synthetic_kr_stock_parity";

export interface MarketParitySource {
  source: string;
  sourceOfTruth: string;
  asOf?: string | null;
  stale: boolean;
  freshnessSec?: number | null;
  warnings: string[];
}

export interface MarketParityCard {
  id: string;
  type: MarketParityCardType;
  title: string;
  baseSymbol?: string | null;
  baseName?: string | null;
  proxySymbol?: string | null;
  syntheticSymbol?: string | null;
  basePrice?: number | null;
  proxyPrice?: number | null;
  syntheticPrice?: number | null;
  fxRate?: number | null;
  usdtKrw?: number | null;
  usdKrw?: number | null;
  impliedValue?: number | null;
  premiumPct?: number | null;
  tone: MarketParityTone;
  formula?: string | null;
  dataState: MarketParityState;
  emptyReason?: string | null;
  source: MarketParitySource;
}

export interface MarketParityResponse {
  market: "kr";
  state: MarketParityState;
  asOf: string;
  cards: MarketParityCard[];
  emptyReason?: string | null;
  warnings: string[];
  notes: string[];
}
