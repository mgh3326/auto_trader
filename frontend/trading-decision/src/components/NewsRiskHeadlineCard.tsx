// frontend/trading-decision/src/components/NewsRiskHeadlineCard.tsx
import type { NewsRadarItem } from "../api/types";
import { formatDateTime } from "../format/datetime";
import styles from "./NewsRiskHeadlineCard.module.css";

export interface NewsRiskHeadlineCardProps {
  item: NewsRadarItem;
}

const SEVERITY_LABEL: Record<NewsRadarItem["severity"], string> = {
  high: "High",
  medium: "Medium",
  low: "Low",
};

export default function NewsRiskHeadlineCard({ item }: NewsRiskHeadlineCardProps) {
  const sourceLabel = item.source ?? item.feed_source ?? "—";
  const inclusionLabel = item.included_in_briefing
    ? "In briefing"
    : "Collected · not in briefing";
  return (
    <article
      className={`${styles.card} ${styles[`severity_${item.severity}`]}`}
      data-testid={`news-radar-card-${item.id}`}
    >
      <header className={styles.header}>
        <span className={styles.severity}>{SEVERITY_LABEL[item.severity]}</span>
        <span className={styles.inclusion}>{inclusionLabel}</span>
      </header>
      <a
        className={styles.title}
        href={item.url}
        rel="noreferrer noopener"
        target="_blank"
      >
        {item.title}
      </a>
      <p className={styles.meta}>
        {sourceLabel} · {formatDateTime(item.published_at)} ·{" "}
        {item.market || "—"}
      </p>
      {item.snippet ? <p className={styles.snippet}>{item.snippet}</p> : null}
      {item.themes.length > 0 ? (
        <ul aria-label="themes" className={styles.chips}>
          {item.themes.map((t) => (
            <li className={styles.chip} key={t}>
              {t}
            </li>
          ))}
        </ul>
      ) : null}
      {item.matched_terms.length > 0 ? (
        <ul aria-label="matched terms" className={styles.chips}>
          {item.matched_terms.map((t) => (
            <li className={`${styles.chip} ${styles.chipMuted}`} key={t}>
              {t}
            </li>
          ))}
        </ul>
      ) : null}
    </article>
  );
}
