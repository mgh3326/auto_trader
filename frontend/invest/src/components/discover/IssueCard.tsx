import { Link } from "react-router-dom";
import type { MarketIssue } from "../../types/newsIssues";
import { Icon, Pill } from "../../ds";
import { describeDirection } from "./severity";
import { formatRelativeTime } from "../../format/relativeTime";

// IssueCard renders an <article> (non-interactive) containing two
// sibling interactive elements: a <Link> on the title (navigates to
// the issue detail page) and a <button> that toggles the inline
// summary. The two sit at the same level in the DOM, so there is no
// nested-interactive HTML violation.
export function IssueCard({
  issue,
  expanded,
  onToggle,
  hrefPrefix = "/discover/issues",
}: {
  issue: MarketIssue;
  expanded: boolean;
  onToggle: () => void;
  hrefPrefix?: string;
}) {
  const dir = describeDirection(issue.direction);
  const ago = formatRelativeTime(issue.updated_at) ?? "방금";
  const summaryId = `issue-card-summary-${issue.id}`;

  return (
    <article
      data-testid="issue-card"
      data-issue-id={issue.id}
      data-direction={issue.direction}
      style={{
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: 16,
        boxShadow: "var(--shadow-1)",
      }}
    >
      <div
        style={{
          display: "flex",
          gap: 14,
          padding: 16,
          alignItems: "flex-start",
        }}
      >
        <div
          style={{
            minWidth: 28,
            fontSize: 18,
            fontWeight: 800,
            color: "var(--fg-3)",
            fontFeatureSettings: '"tnum"',
            paddingTop: 2,
          }}
          aria-label={`순위 ${issue.rank}`}
        >
          {issue.rank}
        </div>

        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ color: dir.color, fontWeight: 700 }} aria-hidden>
              {dir.glyph}
            </span>
            <Link
              to={`${hrefPrefix}/${issue.id}`}
              data-testid="issue-card-detail-link"
              style={{
                fontSize: 15,
                fontWeight: 700,
                color: "var(--fg)",
                textDecoration: "none",
                lineHeight: 1.4,
              }}
            >
              {issue.issue_title}
            </Link>
          </div>
          {issue.subtitle && (
            <div style={{ fontSize: 13, color: "var(--fg-3)", marginTop: 4, lineHeight: 1.45 }}>
              {issue.subtitle}
            </div>
          )}
          <div style={{ fontSize: 12, color: "var(--fg-3)", marginTop: 8, display: "flex", gap: 10 }}>
            <span>{issue.source_count}개 출처</span>
            <span>· 기사 {issue.article_count}개</span>
            <span>· {ago}</span>
          </div>
        </div>

        <button
          type="button"
          data-testid="issue-card-toggle"
          aria-expanded={expanded}
          aria-controls={summaryId}
          aria-label={expanded ? `${issue.issue_title} 요약 접기` : `${issue.issue_title} 요약 더보기`}
          onClick={onToggle}
          style={{
            width: 32,
            height: 32,
            border: "none",
            borderRadius: 8,
            background: "var(--surface-2)",
            color: "var(--fg-2)",
            cursor: "pointer",
            display: "grid",
            placeItems: "center",
            flexShrink: 0,
            transform: expanded ? "rotate(90deg)" : "none",
            transition: "transform 120ms cubic-bezier(0.2,0,0,1)",
            fontFamily: "inherit",
          }}
        >
          <Icon name="chev" size={14} />
        </button>
      </div>

      {expanded && issue.summary && (
        <div
          id={summaryId}
          data-testid="issue-card-summary"
          style={{
            margin: "0 16px 16px",
            paddingTop: 12,
            borderTop: "1px solid var(--divider)",
            fontSize: 14,
            color: "var(--fg-1)",
            lineHeight: 1.6,
          }}
        >
          {issue.summary}
          {issue.related_symbols.length > 0 && (
            <div style={{ marginTop: 10, display: "flex", gap: 6, flexWrap: "wrap" }}>
              {issue.related_symbols.map((s) => (
                <Pill key={`${s.market}:${s.symbol}`} tone="accent" size="sm">
                  {s.canonical_name}
                </Pill>
              ))}
            </div>
          )}
        </div>
      )}
    </article>
  );
}
