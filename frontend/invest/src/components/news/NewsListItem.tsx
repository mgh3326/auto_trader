import { useId } from "react";
import { Link } from "react-router-dom";
import { formatRelativeTime } from "../../format/relativeTime";
import type { FeedNewsItem, FeedRelatedSymbol } from "../../types/feedNews";
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

interface NewsListItemProps {
  item: FeedNewsItem;
  issue?: MarketIssue;
  open: boolean;
  onToggle: () => void;
  discoverIssueHrefPrefix?: string;
  variant?: "desktop" | "mobile";
}

function displaySource(item: FeedNewsItem): string {
  return item.publisher?.trim() || item.feedSource?.trim() || "출처 미상";
}

function formatChangeRate(rate: number | null | undefined): string | null {
  if (typeof rate !== "number" || !Number.isFinite(rate)) return null;
  const sign = rate > 0 ? "+" : "";
  return `${sign}${rate.toFixed(2)}%`;
}

function changeRateColor(rate: number): string {
  if (rate > 0) return "var(--gain)";
  if (rate < 0) return "var(--loss)";
  return "var(--fg-3)";
}

function relationTone(relation: FeedNewsItem["relation"]): "accent" | "kis" {
  return relation === "held" || relation === "both" ? "accent" : "kis";
}

function SymbolChip({ symbol }: { symbol: FeedRelatedSymbol }) {
  const rateText = formatChangeRate(symbol.quote?.changeRate);

  return (
    <span
      data-testid="feed-item-related-symbol-chip"
      data-symbol={symbol.symbol}
      data-market={symbol.market}
      data-relation={symbol.relation ?? "none"}
      title={symbol.matchedTerm ? `${symbol.matchReason ?? "matched"}: ${symbol.matchedTerm}` : undefined}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        minHeight: 24,
        padding: "2px 8px",
        borderRadius: 999,
        border: "1px solid var(--border)",
        background: "var(--surface-2)",
        color: "var(--fg-2)",
        fontSize: 11,
        fontFamily: "var(--font-mono)",
        whiteSpace: "nowrap",
      }}
    >
      <strong style={{ color: "var(--fg)", fontWeight: 800 }}>{symbol.symbol}</strong>
      <span style={{ fontFamily: "var(--font-sans)", overflow: "hidden", textOverflow: "ellipsis" }}>
        {symbol.displayName}
      </span>
      {symbol.relation && symbol.relation !== "none" && (
        <span style={{ fontFamily: "var(--font-sans)", color: "var(--fg-3)" }}>
          {RELATION_LABEL[symbol.relation] ?? symbol.relation}
        </span>
      )}
      {rateText && (
        <span
          data-testid="feed-item-symbol-change-rate"
          style={{
            fontFamily: "var(--font-sans)",
            color: changeRateColor(symbol.quote?.changeRate ?? 0),
            fontWeight: 800,
          }}
        >
          {rateText}
        </span>
      )}
    </span>
  );
}

export function NewsListItem({
  item,
  issue,
  open,
  onToggle,
  discoverIssueHrefPrefix = "/discover/issues",
  variant = "desktop",
}: NewsListItemProps) {
  const summaryId = useId();
  const relationLabel = item.relation !== "none" ? RELATION_LABEL[item.relation] : null;
  const ago = formatRelativeTime(item.publishedAt) ?? "시간 미상";
  const source = displaySource(item);
  const summaryButtonLabel = open ? `${item.title} 요약 접기` : `${item.title} 요약 더보기`;
  const hasSummary = Boolean(item.summarySnippet);
  const issueHref = issue ? `${discoverIssueHrefPrefix}/${encodeURIComponent(issue.id)}` : null;

  return (
    <li
      data-testid="feed-item"
      data-relation={item.relation}
      style={{ listStyle: "none" }}
    >
      <article
        style={{
          background: "var(--surface)",
          border: "1px solid var(--border)",
          borderRadius: 16,
          boxShadow: "var(--shadow-1)",
          padding: variant === "mobile" ? "12px 13px" : "13px 15px",
        }}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              flexWrap: "wrap",
              color: "var(--fg-3)",
              fontSize: 12,
              lineHeight: 1.35,
            }}
          >
            <span>{source}</span>
            <span aria-hidden>·</span>
            <span>{MARKET_LABEL[item.market]}</span>
            <span aria-hidden>·</span>
            <span>{ago}</span>
            {relationLabel && (
              <Pill tone={relationTone(item.relation)} size="sm">
                {relationLabel}
              </Pill>
            )}
          </div>

          <div style={{ display: "flex", alignItems: "flex-start", gap: 10 }}>
            <a
              href={item.url}
              target="_blank"
              rel="noreferrer"
              style={{
                flex: 1,
                minWidth: 0,
                color: "var(--fg)",
                fontSize: variant === "mobile" ? 15 : 16,
                fontWeight: 800,
                lineHeight: 1.38,
                letterSpacing: "-0.02em",
                textDecoration: "none",
              }}
            >
              {item.title}
            </a>
            <button
              type="button"
              onClick={onToggle}
              disabled={!hasSummary}
              aria-expanded={open}
              aria-controls={summaryId}
              aria-label={summaryButtonLabel}
              style={{
                flex: "0 0 auto",
                minWidth: 40,
                minHeight: 36,
                border: "1px solid var(--border)",
                borderRadius: 999,
                background: hasSummary ? "var(--surface-2)" : "transparent",
                color: hasSummary ? "var(--fg-2)" : "var(--fg-3)",
                cursor: hasSummary ? "pointer" : "default",
                fontFamily: "inherit",
                fontSize: 12,
                fontWeight: 800,
              }}
            >
              {open ? "접기" : "요약"}
            </button>
          </div>

          {(item.relatedSymbols.length > 0 || issue) && (
            <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
              {item.relatedSymbols.length > 0 && (
                <div
                  data-testid="feed-item-related-symbols"
                  style={{ display: "flex", flexWrap: "wrap", gap: 6, minWidth: 0 }}
                >
                  {item.relatedSymbols.map((symbol) => (
                    <SymbolChip key={`${symbol.market}:${symbol.symbol}`} symbol={symbol} />
                  ))}
                </div>
              )}

              {issue && (
                <Link
                  to={issueHref!}
                  data-testid="feed-item-issue-chip"
                  data-issue-id={issue.id}
                  aria-label={`이슈 링크: ${issue.issue_title}`}
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 6,
                    minHeight: 24,
                    padding: "2px 10px",
                    borderRadius: 999,
                    background: "var(--accent-soft)",
                    color: "var(--accent-press)",
                    fontSize: 11,
                    fontWeight: 700,
                    textDecoration: "none",
                    maxWidth: "100%",
                  }}
                >
                  <span aria-hidden style={{ fontSize: 9 }}>
                    ●
                  </span>
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    이슈 · {issue.issue_title}
                  </span>
                </Link>
              )}
            </div>
          )}
        </div>

        {open && item.summarySnippet && (
          <div
            id={summaryId}
            data-testid="feed-item-summary"
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
      </article>
    </li>
  );
}
