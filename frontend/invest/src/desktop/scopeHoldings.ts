import type { AccountSource, GroupedHolding } from "../types/invest";

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
    let qtyForAvg = 0;
    let costSum = 0;

    for (const b of slices) {
      totalQuantity += b.quantity;
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
      pnlKrw != null && costBasis != null && costBasis !== 0
        ? pnlKrw / costBasis
        : null;

    out.push({
      ...g,
      totalQuantity,
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
