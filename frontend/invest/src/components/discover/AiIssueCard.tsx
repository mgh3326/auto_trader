// frontend/invest/src/components/discover/AiIssueCard.tsx
import { Link } from "react-router-dom";
import { formatRelativeTime } from "../../format/relativeTime";
import type { NewsRadarItem } from "../../types/newsRadar";
import { describeSeverity } from "./severity";

export type AiIssueCardProps = Readonly<{
  rank: number;
  item: NewsRadarItem;
  relatedCount: number;
  now?: Date;
}>;

function buildSubtitle(item: NewsRadarItem): string {
  if (item.snippet && item.snippet.trim().length > 0) return item.snippet;
  if (item.themes.length > 0) return item.themes.join(", ");
  if (item.matched_terms.length > 0) return item.matched_terms.join(", ");
  return "";
}

export function AiIssueCard({ rank, item, relatedCount, now }: AiIssueCardProps) {
  const indicator = describeSeverity(item.severity);
  const time = formatRelativeTime(item.published_at, now);
  const subtitle = buildSubtitle(item);
  return (
    <Link
      to={`/discover/issues/${item.id}`}
      style={{
        display: "flex",
        gap: 12,
        padding: 14,
        background: "var(--surface)",
        border: "1px solid var(--surface-2)",
        borderRadius: 14,
        color: "var(--text)",
        textDecoration: "none",
      }}
    >
      <div
        style={{
          minWidth: 24,
          fontWeight: 800,
          color: "var(--muted)",
          fontSize: 16,
        }}
      >
        {rank}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span
            aria-label={indicator.label}
            role="img"
            style={{ color: indicator.color, fontSize: 12 }}
          >
            {indicator.glyph}
          </span>
          <span style={{ fontWeight: 700, fontSize: 14 }}>{item.title}</span>
        </div>
        {subtitle && (
          <div
            className="subtle"
            style={{
              marginTop: 4,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {subtitle}
          </div>
        )}
        <div
          className="subtle"
          style={{ marginTop: 6, display: "flex", gap: 8, fontSize: 11 }}
        >
          <span>관련 뉴스 {relatedCount}개</span>
          {time && <span>· {time}</span>}
        </div>
      </div>
    </Link>
  );
}
