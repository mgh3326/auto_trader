import { formatRelativeTime } from "../../format/relativeTime";
import { Pill } from "../../ds";
import type { FeedResearchItem, ResearchMarket, ResearchSymbolCandidate } from "../../types/feedResearch";

const MARKET_LABEL: Record<ResearchMarket, string> = {
  kr: "KR",
  us: "US",
  crypto: "CRYPTO",
};

const RELATION_LABEL = {
  mine: "보유",
  watch: "관심",
} as const;

interface ResearchListItemProps {
  item: FeedResearchItem;
  variant?: "desktop" | "mobile";
}

function clean(value: string | null | undefined): string | null {
  const trimmed = value?.trim();
  return trimmed ? trimmed : null;
}

function displayDate(item: FeedResearchItem): string {
  return formatRelativeTime(item.publishedAt) ?? clean(item.publishedAtText) ?? "시간 미상";
}

function displaySymbol(candidate: ResearchSymbolCandidate): string | null {
  return clean(candidate.displayName) ?? clean(candidate.name);
}

function displayMarketLabel(market: ResearchSymbolCandidate["market"]): string {
  if (market === "kr" || market === "us" || market === "crypto") return MARKET_LABEL[market];
  return clean(market) ?? "UNKNOWN";
}

function safeExternalUrl(value: string | null | undefined): string | null {
  const trimmed = clean(value);
  if (!trimmed) return null;
  try {
    const url = new URL(trimmed);
    return url.protocol === "https:" || url.protocol === "http:" ? trimmed : null;
  } catch {
    return null;
  }
}

function SymbolCandidateChip({ candidate }: { candidate: ResearchSymbolCandidate }) {
  const marketLabel = displayMarketLabel(candidate.market);
  const market = clean(candidate.market);
  const name = displaySymbol(candidate);
  return (
    <span
      data-testid="research-symbol-chip"
      data-symbol={candidate.symbol}
      data-market={market ?? undefined}
      title={clean(candidate.reason) ?? clean(candidate.source) ?? undefined}
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
      <strong style={{ color: "var(--fg)", fontWeight: 800 }}>{candidate.symbol}</strong>
      <span style={{ color: "var(--fg-3)", fontFamily: "var(--font-sans)" }}>· {marketLabel}</span>
      {name && (
        <span style={{ fontFamily: "var(--font-sans)", overflow: "hidden", textOverflow: "ellipsis" }}>{name}</span>
      )}
    </span>
  );
}

export function ResearchListItem({ item, variant = "desktop" }: ResearchListItemProps) {
  const source = clean(item.source) ?? "출처 미상";
  const category = clean(item.category);
  const analyst = clean(item.analyst);
  const excerpt = clean(item.excerpt);
  const relationLabel = item.relation === "none" ? null : RELATION_LABEL[item.relation];
  const attributionPublisher = clean(item.attributionPublisher);
  const attributionNotice = clean(item.attributionCopyrightNotice);
  const title = clean(item.title) ?? "제목 없는 리서치";
  const symbolCandidates = item.symbolCandidates ?? [];
  const detailUrl = safeExternalUrl(item.detailUrl);
  const pdfUrl = safeExternalUrl(item.pdfUrl);

  return (
    <li data-testid="research-feed-item" data-relation={item.relation} style={{ listStyle: "none" }}>
      <article
        style={{
          background: "var(--surface)",
          border: "1px solid var(--border)",
          borderRadius: 16,
          boxShadow: "var(--shadow-1)",
          padding: variant === "mobile" ? "12px 13px" : "13px 15px",
        }}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
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
            <Pill tone="accent" size="sm">리서치</Pill>
            <span>{source}</span>
            {category && (
              <>
                <span aria-hidden>·</span>
                <span>{category}</span>
              </>
            )}
            {analyst && (
              <>
                <span aria-hidden>·</span>
                <span>{analyst}</span>
              </>
            )}
            <span aria-hidden>·</span>
            <span>{displayDate(item)}</span>
            {relationLabel && <Pill tone="kis" size="sm">{relationLabel}</Pill>}
          </div>

          <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
            <div
              aria-hidden
              style={{
                flex: "0 0 auto",
                width: variant === "mobile" ? 52 : 60,
                height: variant === "mobile" ? 52 : 60,
                borderRadius: 14,
                background: "linear-gradient(135deg, var(--accent-soft), var(--surface-2))",
                border: "1px solid var(--border)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                color: "var(--accent-press)",
                fontSize: 18,
                fontWeight: 800,
              }}
            >
              R
            </div>
            {detailUrl ? (
              <a
                href={detailUrl}
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
                {title}
              </a>
            ) : (
              <strong
                style={{
                  flex: 1,
                  minWidth: 0,
                  color: "var(--fg)",
                  fontSize: variant === "mobile" ? 15 : 16,
                  fontWeight: 800,
                  lineHeight: 1.38,
                  letterSpacing: "-0.02em",
                }}
              >
                {title}
              </strong>
            )}
          </div>

          {excerpt && (
            <p style={{ margin: 0, color: "var(--fg-2)", fontSize: 13, lineHeight: 1.55 }}>{excerpt}</p>
          )}

          {(pdfUrl || symbolCandidates.length > 0) && (
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              {pdfUrl && (
                <a
                  href={pdfUrl}
                  target="_blank"
                  rel="noreferrer"
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    minHeight: 24,
                    padding: "2px 10px",
                    borderRadius: 999,
                    background: "var(--surface-2)",
                    border: "1px solid var(--border)",
                    color: "var(--fg-2)",
                    fontSize: 12,
                    fontWeight: 700,
                    textDecoration: "none",
                  }}
                >
                  원문 PDF
                </a>
              )}
              {symbolCandidates.map((candidate) => (
                <SymbolCandidateChip key={`${candidate.market}:${candidate.symbol}`} candidate={candidate} />
              ))}
            </div>
          )}

          {(attributionPublisher || attributionNotice) && (
            <footer
              style={{
                borderTop: "1px solid var(--divider)",
                paddingTop: 8,
                color: "var(--fg-3)",
                fontSize: 11,
                lineHeight: 1.45,
              }}
            >
              {[attributionPublisher, attributionNotice].filter(Boolean).join(" · ")}
            </footer>
          )}
        </div>
      </article>
    </li>
  );
}
