// frontend/invest/src/components/discover/AiIssueCard.tsx
import { Link } from "react-router-dom";
import { formatRelativeTime } from "../../format/relativeTime";
import type { MarketIssue } from "../../types/newsIssues";
import { describeDirection } from "./severity";

export type AiIssueCardProps = Readonly<{
  issue: MarketIssue;
  now?: Date;
}>;

function buildSubtitle(issue: MarketIssue): string {
  if (issue.subtitle && issue.subtitle.trim().length > 0) return issue.subtitle;
  if (issue.summary && issue.summary.trim().length > 0) return issue.summary;
  if (issue.related_sectors.length > 0) return issue.related_sectors.join(", ");
  return "";
}

export function AiIssueCard({ issue, now }: AiIssueCardProps) {
  const indicator = describeDirection(issue.direction);
  const time = formatRelativeTime(issue.updated_at, now);
  const subtitle = buildSubtitle(issue);
  return (
    <Link
      to={`/discover/issues/${issue.id}`}
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
        {issue.rank}
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
          <span style={{ fontWeight: 700, fontSize: 14 }}>{issue.issue_title}</span>
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
          <span>{issue.source_count}개 출처</span>
          <span>· 기사 {issue.article_count}개</span>
          {time && <span>· {time}</span>}
        </div>
      </div>
    </Link>
  );
}
