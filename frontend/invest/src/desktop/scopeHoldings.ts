import type { AccountPanelResponse, AccountSource, CashAmounts, GroupedHolding } from "../types/invest";
import { accountSourceMeta } from "./AccountSourceMeta";

export type AccountFilterKey = "all" | AccountSource;

export interface AccountFilterOption {
  key: AccountFilterKey;
  label: string;
  source?: AccountSource;
  cashBalances: CashAmounts;
  totalValueKrw: number;
  costBasisKrw: number | null;
  pnlKrw: number | null;
  pnlRate: number | null;
  holdingCount: number;
}

export interface ScopedPortfolioPanel {
  selected: AccountFilterOption;
  options: AccountFilterOption[];
  groupedHoldings: GroupedHolding[];
  totalValueKrw: number;
  costBasisKrw: number | null;
  pnlKrw: number | null;
  pnlRate: number | null;
  cashBalances: CashAmounts;
}

// When the user filters the home view to a single source/account, the grouped
// rows must be reduced to that source's slice so the table totals match the
// hero summary and the breakdown chips. We rely on `sourceBreakdown` to do the
// recomputation; if a multi-source group lacks breakdown data we skip it
// rather than misrepresent the full group's totals as a single source.
export function scopeGroupedToSource(
  groups: GroupedHolding[],
  source: AccountSource,
): GroupedHolding[] {
  const out: GroupedHolding[] = [];
  for (const g of groups) {
    if (!g.includedSources.includes(source)) continue;

    // Single-source group — the slice equals the whole group.
    if (g.includedSources.length === 1) {
      out.push(g);
      continue;
    }

    const slices = g.sourceBreakdown.filter((b) => b.source === source);
    if (slices.length === 0) {
      // Multi-source group with no breakdown — cannot safely slice.
      continue;
    }

    let totalQuantity = 0;
    let costBasis: number | null = null;
    let valueNative: number | null = null;
    let valueKrw: number | null = null;
    let pnlKrw: number | null = null;
    let tradeableQuantity = 0;
    let sellableQuantity = 0;
    let pendingSellQuantity = 0;
    let referenceQuantity = 0;
    let qtyForAvg = 0;
    let costSum = 0;

    for (const b of slices) {
      totalQuantity += b.quantity;
      tradeableQuantity += b.isTradeable ? b.quantity : 0;
      sellableQuantity += b.sellableQuantity ?? 0;
      pendingSellQuantity += b.pendingSellQuantity ?? 0;
      referenceQuantity += b.referenceQuantity ?? (b.manualOnly ? b.quantity : 0);
      if (b.costBasis != null) costBasis = (costBasis ?? 0) + b.costBasis;
      if (b.valueNative != null) valueNative = (valueNative ?? 0) + b.valueNative;
      if (b.valueKrw != null) valueKrw = (valueKrw ?? 0) + b.valueKrw;
      if (b.pnlKrw != null) pnlKrw = (pnlKrw ?? 0) + b.pnlKrw;
      if (b.averageCost != null && b.quantity > 0) {
        costSum += b.averageCost * b.quantity;
        qtyForAvg += b.quantity;
      }
    }

    const averageCost = qtyForAvg > 0 ? costSum / qtyForAvg : null;
    const pnlRate =
      valueNative != null && costBasis != null && costBasis !== 0
        ? (valueNative - costBasis) / costBasis
        : null;

    out.push({
      ...g,
      totalQuantity,
      tradeableQuantity,
      sellableQuantity,
      pendingSellQuantity,
      referenceQuantity,
      averageCost,
      costBasis,
      valueNative,
      valueKrw,
      pnlKrw,
      pnlRate,
      includedSources: [source],
      sourceBreakdown: slices,
    });
  }
  return out;
}

function sourceLabel(response: AccountPanelResponse, source: AccountSource): string {
  const meta = accountSourceMeta(source);
  const account = response.accounts.find((a) => a.source === source);
  if (account?.displayName && (source === "alpaca_paper" || source === "db_simulated" || source === "kiwoom_mock")) {
    if (account.displayName.includes(meta.label) || account.displayName.includes(meta.shortLabel)) return account.displayName;
  }
  return meta.label;
}

function sumCash(accounts: AccountPanelResponse["accounts"]): CashAmounts {
  let krw: number | null = null;
  let usd: number | null = null;
  for (const account of accounts) {
    const accountKrw = account.cashBalances?.krw;
    const accountUsd = account.cashBalances?.usd;
    if (accountKrw != null) krw = (krw ?? 0) + accountKrw;
    if (accountUsd != null) usd = (usd ?? 0) + accountUsd;
  }
  return { krw, usd };
}

function groupCostBasisKrw(group: GroupedHolding): number | null {
  if (group.costBasis == null) return null;
  if (group.currency === "KRW") return group.costBasis;
  if (group.currency === "USD") {
    if (group.valueKrw != null && group.pnlKrw != null) return group.valueKrw - group.pnlKrw;
    if (group.valueKrw != null && group.valueNative != null && group.valueNative > 0) {
      return group.costBasis * (group.valueKrw / group.valueNative);
    }
  }
  return null;
}

function summarizeHoldings(groups: GroupedHolding[]): Pick<AccountFilterOption, "totalValueKrw" | "costBasisKrw" | "pnlKrw" | "pnlRate" | "holdingCount"> {
  let totalValueKrw = 0;
  let costBasisKrw: number | null = null;
  let pnlKrw: number | null = null;

  for (const group of groups) {
    if (group.valueKrw != null) totalValueKrw += group.valueKrw;
    const groupCostKrw = groupCostBasisKrw(group);
    if (groupCostKrw != null) costBasisKrw = (costBasisKrw ?? 0) + groupCostKrw;
    if (group.pnlKrw != null) pnlKrw = (pnlKrw ?? 0) + group.pnlKrw;
  }

  const pnlRate =
    pnlKrw != null && costBasisKrw != null && costBasisKrw > 0
      ? pnlKrw / costBasisKrw
      : null;

  return {
    totalValueKrw,
    costBasisKrw,
    pnlKrw,
    pnlRate,
    holdingCount: groups.length,
  };
}

function optionFor(response: AccountPanelResponse, key: AccountFilterKey): AccountFilterOption {
  if (key === "all") {
    return {
      key: "all",
      label: "전체",
      cashBalances: sumCash(response.accounts.filter((account) => account.includedInHome)),
      totalValueKrw: response.homeSummary.totalValueKrw,
      costBasisKrw: response.homeSummary.costBasisKrw ?? null,
      pnlKrw: response.homeSummary.pnlKrw ?? null,
      pnlRate: response.homeSummary.pnlRate ?? null,
      holdingCount: response.groupedHoldings.length,
    };
  }

  const scoped = scopeGroupedToSource(response.groupedHoldings, key);
  const summary = summarizeHoldings(scoped);
  return {
    key,
    source: key,
    label: sourceLabel(response, key),
    cashBalances: sumCash(response.accounts.filter((a) => a.source === key)),
    ...summary,
  };
}

export function buildAccountFilterOptions(response: AccountPanelResponse): AccountFilterOption[] {
  const sources = new Set<AccountSource>();
  for (const account of response.accounts) {
    sources.add(account.source);
  }
  for (const holding of response.groupedHoldings) {
    for (const source of holding.includedSources) {
      sources.add(source);
    }
  }

  return [
    optionFor(response, "all"),
    ...Array.from(sources).map((source) => optionFor(response, source)),
  ];
}

export function buildScopedPortfolioPanel(
  response: AccountPanelResponse,
  selectedKey: AccountFilterKey,
): ScopedPortfolioPanel {
  const options = buildAccountFilterOptions(response);
  const selected = options.find((option) => option.key === selectedKey) ?? options[0]!;
  const groupedHoldings = selected.key === "all"
    ? response.groupedHoldings
    : scopeGroupedToSource(response.groupedHoldings, selected.key);

  return {
    selected,
    options,
    groupedHoldings,
    totalValueKrw: selected.totalValueKrw,
    costBasisKrw: selected.costBasisKrw,
    pnlKrw: selected.pnlKrw,
    pnlRate: selected.pnlRate,
    cashBalances: selected.cashBalances,
  };
}
