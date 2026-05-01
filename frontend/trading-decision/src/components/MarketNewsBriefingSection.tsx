import type {
  PreopenMarketNewsBriefing,
  PreopenMarketNewsItem,
} from "../api/types";
import { formatDateTime } from "../format/datetime";
import styles from "./MarketNewsBriefingSection.module.css";

export interface MarketNewsBriefingSectionProps {
  briefing: PreopenMarketNewsBriefing | null;
}

type SummaryKey = "included" | "excluded" | "sections" | "uncategorized";

function summaryNumber(
  summary: Record<string, unknown>,
  key: SummaryKey,
): number | null {
  const value = summary[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function NewsItem({ item }: { item: PreopenMarketNewsItem }) {
  const relevance = item.briefing_relevance;
  const matchedTerms = relevance?.matched_terms ?? [];

  return (
    <li className={styles.item}>
      <a
        className={styles.itemTitle}
        href={item.url}
        rel="noreferrer noopener"
        target="_blank"
      >
        {item.title}
      </a>
      <span className={styles.itemMeta}>
        {item.source ?? item.feed_source ?? "—"} ·{" "}
        {formatDateTime(item.published_at)}
      </span>
      {item.summary ? (
        <p className={styles.itemSummary}>{item.summary}</p>
      ) : null}
      {relevance ? (
        <div className={styles.relevance}>
          <span className={styles.score}>Score {relevance.score}</span>
          {matchedTerms.length > 0 ? (
            <span className={styles.terms}>Terms: {matchedTerms.join(", ")}</span>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

export default function MarketNewsBriefingSection({
  briefing,
}: MarketNewsBriefingSectionProps) {
  if (briefing === null) {
    return (
      <section aria-label="Market news briefing" className={styles.section}>
        <header className={styles.header}>
          <h2>Market news briefing</h2>
        </header>
        <p className={styles.muted}>No market news briefing available yet.</p>
      </section>
    );
  }

  const summaryChips = (
    ["included", "excluded", "sections", "uncategorized"] as const
  )
    .map((key) => [key, summaryNumber(briefing.summary, key)] as const)
    .filter(
      (entry): entry is readonly [SummaryKey, number] => entry[1] !== null,
    );

  return (
    <section aria-label="Market news briefing" className={styles.section}>
      <header className={styles.header}>
        <div>
          <h2>Market news briefing</h2>
          <p className={styles.subtitle}>
            Market-aware sections from recent news, filtered before trading
            review.
          </p>
        </div>
      </header>

      {summaryChips.length > 0 ? (
        <ul aria-label="Market news briefing summary" className={styles.summaryChips}>
          {summaryChips.map(([key, value]) => (
            <li className={styles.summaryChip} key={key}>
              <span>{key}</span>
              <strong>{value}</strong>
            </li>
          ))}
        </ul>
      ) : null}

      {briefing.sections.length === 0 ? (
        <p className={styles.muted}>No high-signal briefing sections found.</p>
      ) : (
        <div className={styles.sectionGrid}>
          {briefing.sections.map((section) => (
            <article className={styles.card} key={section.section_id}>
              <header className={styles.cardHeader}>
                <h3>{section.title}</h3>
                <span className={styles.count}>{section.items.length} items</span>
              </header>
              {section.items.length === 0 ? (
                <p className={styles.muted}>No articles in this section.</p>
              ) : (
                <ul className={styles.itemList}>
                  {section.items.map((item) => (
                    <NewsItem item={item} key={item.id} />
                  ))}
                </ul>
              )}
            </article>
          ))}
        </div>
      )}

      <div className={styles.excludedLine}>
        Filtered noise: {briefing.excluded_count}
      </div>
      {briefing.top_excluded.length > 0 ? (
        <details className={styles.excludedDetails}>
          <summary>Show top excluded articles ({briefing.top_excluded.length})</summary>
          <ul className={styles.itemList}>
            {briefing.top_excluded.map((item) => (
              <NewsItem item={item} key={item.id} />
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  );
}
