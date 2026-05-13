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

// ROB-172: MARKET_LABEL is shared by the chip (asset market) and the header
// source-market line, which now reads `item.sourceMarket ?? item.market`.
// A follow-up ticket can drop the `market` fallback once the backend dual-emit
// window closes and all clients consume `sourceMarket`.
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

function symbolChangePct(symbol: FeedRelatedSymbol): number | null {
  if (typeof symbol.changePct === "number" && Number.isFinite(symbol.changePct)) return symbol.changePct;
  const quoteRate = symbol.quote?.changeRate;
  return typeof quoteRate === "number" && Number.isFinite(quoteRate) ? quoteRate : null;
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
  const changePct = symbolChangePct(symbol);
  const rateText = formatChangeRate(changePct);
  const marketLabel = MARKET_LABEL[symbol.market];

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
      <span
        data-testid="feed-item-symbol-market"
        style={{ color: "var(--fg-3)", fontFamily: "var(--font-sans)" }}
      >
        · {marketLabel}
      </span>
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
            color: changeRateColor(changePct ?? 0),
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
  const feedMarket = item.sourceMarket ?? item.market;
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
            <span data-testid="feed-item-source-market">{MARKET_LABEL[feedMarket]}</span>
            <span aria-hidden>·</span>
            <span>{ago}</span>
            {relationLabel && (
              <Pill tone={relationTone(item.relation)} size="sm">
                {relationLabel}
              </Pill>
            )}
          </div>

          <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
            <div
              aria-hidden
              data-testid="feed-item-thumbnail-placeholder"
              style={{
                flex: "0 0 auto",
                width: variant === "mobile" ? 52 : 60,
                height: variant === "mobile" ? 52 : 60,
                borderRadius: 14,
                background: "linear-gradient(135deg, var(--surface-2), var(--surface-3, var(--surface-2)))",
                border: "1px solid var(--border)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                color: "var(--fg-3)",
                fontSize: 18,
                fontWeight: 800,
              }}
            >
              N
            </div>
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
          </div>

          {(item.relatedSymbols.length > 0 || issue || hasSummary) && (
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              {hasSummary && (
                <button
                  type="button"
                  onClick={onToggle}
                  aria-expanded={open}
                  aria-controls={summaryId}
                  aria-label={summaryButtonLabel}
                  style={{
                    border: "none",
                    background: "transparent",
                    color: "var(--fg-3)",
                    cursor: "pointer",
                    fontFamily: "inherit",
                    fontSize: 12,
                    fontWeight: 700,
                    padding: "2px 0",
                  }}
                >
                  {open ? "요약 접기" : "요약 보기"}
                </button>
              )}
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
