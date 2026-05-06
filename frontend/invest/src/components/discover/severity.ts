// frontend/invest/src/components/discover/severity.ts
import type { NewsRadarItem, NewsRadarSeverity } from "../../types/newsRadar";

export interface SeverityDescriptor {
  label: string;
  color: string;
  glyph: "▲" | "■" | "·";
}

export function describeSeverity(severity: NewsRadarSeverity): SeverityDescriptor {
  switch (severity) {
    case "high":
      return { label: "강한 이슈", color: "var(--gain)", glyph: "▲" };
    case "medium":
      return { label: "관심 이슈", color: "var(--muted)", glyph: "■" };
    case "low":
    default:
      return { label: "참고", color: "var(--muted)", glyph: "·" };
  }
}

export type RiskBucketKey =
  | "geopolitical_oil"
  | "macro_policy"
  | "crypto_security"
  | "earnings_bigtech"
  | "korea_market"
  | "uncategorized";

export function countByRiskCategory(items: NewsRadarItem[]): Record<RiskBucketKey, number> {
  const out: Record<RiskBucketKey, number> = {
    geopolitical_oil: 0,
    macro_policy: 0,
    crypto_security: 0,
    earnings_bigtech: 0,
    korea_market: 0,
    uncategorized: 0,
  };
  for (const item of items) {
    const key = (item.risk_category ?? "uncategorized") as RiskBucketKey;
    out[key] = (out[key] ?? 0) + 1;
  }
  return out;
}

const SEVERITY_RANK: Record<NewsRadarSeverity, number> = { high: 3, medium: 2, low: 1 };

export function sortIssueItems(items: NewsRadarItem[]): NewsRadarItem[] {
  return [...items].sort((a, b) => {
    const sev = SEVERITY_RANK[b.severity] - SEVERITY_RANK[a.severity];
    if (sev !== 0) return sev;
    if (b.briefing_score !== a.briefing_score) return b.briefing_score - a.briefing_score;
    const at = a.published_at ? Date.parse(a.published_at) : 0;
    const bt = b.published_at ? Date.parse(b.published_at) : 0;
    return bt - at;
  });
}

export function relatedNewsCount(
  item: NewsRadarItem,
  buckets: Record<RiskBucketKey, number>,
): number {
  const key = (item.risk_category ?? "uncategorized") as RiskBucketKey;
  return buckets[key] ?? 0;
}
