export type AccountKind = "live" | "manual" | "paper";
export type AccountSource =
  | "kis"
  | "upbit"
  | "toss_manual"
  | "pension_manual"
  | "isa_manual"
  | "kis_mock"
  | "kiwoom_mock"
  | "alpaca_paper"
  | "db_simulated";
export type Market = "KR" | "US" | "CRYPTO";
export type AssetType = "equity" | "etf" | "crypto" | "fund" | "other";
export type Currency = "KRW" | "USD";

export interface CashAmounts {
  krw?: number | null;
  usd?: number | null;
}

export interface Account {
  accountId: string;
  displayName: string;
  source: AccountSource;
  accountKind: AccountKind;
  includedInHome: boolean;
  valueKrw: number;
  costBasisKrw?: number | null;
  pnlKrw?: number | null;
  pnlRate?: number | null;
  cashBalances: CashAmounts;
  buyingPower: CashAmounts;
}

export interface Holding {
  holdingId: string;
  accountId: string;
  source: AccountSource;
  accountKind: AccountKind;
  symbol: string;
  market: Market;
  assetType: AssetType;
  displayName: string;
  quantity: number;
  averageCost?: number | null;
  costBasis?: number | null;
  currency: Currency;
  valueNative?: number | null;
  valueKrw?: number | null;
  pnlKrw?: number | null;
  pnlRate?: number | null;
}

export interface GroupedSourceBreakdown {
  holdingId: string;
  accountId: string;
  source: AccountSource;
  quantity: number;
  averageCost?: number | null;
  costBasis?: number | null;
  valueNative?: number | null;
  valueKrw?: number | null;
  pnlKrw?: number | null;
  pnlRate?: number | null;
}

export interface GroupedHolding {
  groupId: string;
  symbol: string;
  market: Market;
  assetType: AssetType;
  displayName: string;
  currency: Currency;
  totalQuantity: number;
  averageCost?: number | null;
  costBasis?: number | null;
  valueNative?: number | null;
  valueKrw?: number | null;
  pnlKrw?: number | null;
  pnlRate?: number | null;
  includedSources: AccountSource[];
  sourceBreakdown: GroupedSourceBreakdown[];
}

export interface HomeSummary {
  includedSources: AccountSource[];
  excludedSources: AccountSource[];
  totalValueKrw: number;
  costBasisKrw?: number | null;
  pnlKrw?: number | null;
  pnlRate?: number | null;
}

export interface InvestHomeWarning {
  source: AccountSource;
  message: string;
}

export interface InvestHomeResponseMeta {
  warnings: InvestHomeWarning[];
}

export interface InvestHomeResponse {
  homeSummary: HomeSummary;
  accounts: Account[];
  holdings: Holding[];
  groupedHoldings: GroupedHolding[];
  meta: InvestHomeResponseMeta;
}
