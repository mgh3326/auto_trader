import { Link } from "react-router-dom";
import type { FeedNewsItem } from "../../types/feedNews";
import type { MarketIssue } from "../../types/newsIssues";
import { Pill } from "../../ds";

const RELATION_LABEL: Record<string, string> = {
  held: "보유",
  watchlist: "관심",
  both: "보유·관심",
};

const MARKET_LABEL: Record<FeedNewsItem["market"], string> = {
  kr: "KR",
  us: "US",
  crypto: "CRYPTO",
};

interface NewsCardProps {
  item: FeedNewsItem;
  issue?: MarketIssue;
  open: boolean;
  onToggle: () => void;
  // Path prefix to the canonical discover route that owns issue detail pages.
  // The legacy /app/discover/issues/:id path remains reachable while Stage 6
  // is pending so existing bookmarks keep working, but new internal links
  // point at /discover/issues/:id.
  discoverIssueHrefPrefix?: string;
}

export function NewsCard({
  item,
  issue,
  open,
  onToggle,
  discoverIssueHrefPrefix = "/discover/issues",
}: NewsCardProps) {
  const relationLabel = item.relation !== "none" ? RELATION_LABEL[item.relation] : null;
  const relationTone = item.relation === "held" || item.relation === "both" ? "accent" : "kis";

  return (
    <li
      data-testid="feed-item"
      data-relation={item.relation}
      style={{
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: 14,
        boxShadow: "var(--shadow-1)",
        padding: 16,
        listStyle: "none",
      }}
    >
      <button
        type="button"
        onClick={onToggle}
        style={{
          background: "none",
          border: "none",
          cursor: "pointer",
          fontFamily: "inherit",
          color: "var(--fg)",
          padding: 0,
          width: "100%",
          textAlign: "left",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          {relationLabel && (
            <Pill tone={relationTone} size="sm">
              {relationLabel}
            </Pill>
          )}
          <div style={{ fontSize: 15, fontWeight: 700, lineHeight: 1.4, flex: 1, minWidth: 0 }}>{item.title}</div>
        </div>
        <div style={{ fontSize: 12, color: "var(--fg-3)", marginTop: 6 }}>
          {item.publisher ?? "—"} · {MARKET_LABEL[item.market]}
        </div>
      </button>

      {issue && (
        <Link
          to={`${discoverIssueHrefPrefix}/${issue.id}`}
          data-testid="feed-item-issue-chip"
          data-issue-id={issue.id}
          aria-label={`이슈 링크: ${issue.issue_title}`}
          onClick={(e) => e.stopPropagation()}
          style={{
            marginTop: 8,
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            padding: "2px 10px",
            borderRadius: 999,
            background: "var(--accent-soft)",
            color: "var(--accent-press)",
            fontSize: 11,
            fontWeight: 600,
            textDecoration: "none",
            maxWidth: "100%",
          }}
        >
          <span aria-hidden style={{ fontSize: 9 }}>●</span>
          <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            이슈 · {issue.issue_title}
          </span>
        </Link>
      )}

      {item.relatedSymbols.length > 0 && (
        <div
          data-testid="feed-item-related-symbols"
          style={{ marginTop: 8, display: "flex", flexWrap: "wrap", gap: 6 }}
        >
          {item.relatedSymbols.map((symbol) => (
            <span
              key={`${symbol.market}:${symbol.symbol}`}
              data-testid="feed-item-related-symbol-chip"
              data-symbol={symbol.symbol}
              data-market={symbol.market}
              data-relation={symbol.relation ?? "none"}
              title={
                symbol.matchedTerm
                  ? `${symbol.matchReason ?? "matched"}: ${symbol.matchedTerm}`
                  : undefined
              }
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 4,
                padding: "2px 8px",
                borderRadius: 999,
                border: "1px solid var(--border)",
                background: "var(--surface-2)",
                color: "var(--fg-2)",
                fontSize: 11,
                fontFamily: "var(--font-mono)",
              }}
            >
              <strong style={{ color: "var(--fg)", fontWeight: 700 }}>{symbol.symbol}</strong>
              <span style={{ fontFamily: "var(--font-sans)" }}>{symbol.displayName}</span>
              {symbol.relation && symbol.relation !== "none" && (
                <span style={{ fontFamily: "var(--font-sans)" }}>[{symbol.relation}]</span>
              )}
            </span>
          ))}
        </div>
      )}

      {open && item.summarySnippet && (
        <div
          style={{
            marginTop: 10,
            paddingTop: 10,
            borderTop: "1px solid var(--divider)",
            fontSize: 13,
            color: "var(--fg-2)",
            lineHeight: 1.55,
          }}
        >
          {item.summarySnippet}
        </div>
      )}
    </li>
  );
}
