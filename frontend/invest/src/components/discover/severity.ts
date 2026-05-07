// frontend/invest/src/components/discover/severity.ts
import type { IssueDirection, MarketIssue } from "../../types/newsIssues";

export interface DirectionDescriptor {
  label: string;
  color: string;
  glyph: "▲" | "▼" | "◆" | "·";
}

export function describeDirection(direction: IssueDirection): DirectionDescriptor {
  switch (direction) {
    case "up":
      return { label: "상승 이슈", color: "var(--gain)", glyph: "▲" };
    case "down":
      return { label: "하락 이슈", color: "var(--loss)", glyph: "▼" };
    case "mixed":
      return { label: "혼조 이슈", color: "var(--warn)", glyph: "◆" };
    case "neutral":
    default:
      return { label: "중립 이슈", color: "var(--muted)", glyph: "·" };
  }
}

export function sortMarketIssues(items: MarketIssue[]): MarketIssue[] {
  return [...items].sort((a, b) => {
    if (a.rank !== b.rank) return a.rank - b.rank;
    const bScore = b.signals.mention_score + b.signals.recency_score + b.signals.source_diversity_score;
    const aScore = a.signals.mention_score + a.signals.recency_score + a.signals.source_diversity_score;
    if (bScore !== aScore) return bScore - aScore;
    return Date.parse(b.updated_at) - Date.parse(a.updated_at);
  });
}
