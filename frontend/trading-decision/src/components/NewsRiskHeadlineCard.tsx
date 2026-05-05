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

const ENTITY_MAP: Record<string, string> = {
  amp: "&",
  apos: "'",
  gt: ">",
  lt: "<",
  nbsp: " ",
  quot: '\"',
};

function decodeHtmlEntities(value: string): string {
  return value.replace(/&(#x?[0-9a-f]+|[a-z]+);/gi, (match, entity: string) => {
    const normalized = entity.toLowerCase();
    if (normalized.startsWith("#x")) {
      return String.fromCodePoint(Number.parseInt(normalized.slice(2), 16));
    }
    if (normalized.startsWith("#")) {
      return String.fromCodePoint(Number.parseInt(normalized.slice(1), 10));
    }
    return ENTITY_MAP[normalized] ?? match;
  });
}

function stripHtml(value: string | null): string | null {
  if (!value) {
    return null;
  }
  const text = decodeHtmlEntities(value)
    .replace(/<[^>]*>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return text || null;
}

export default function NewsRiskHeadlineCard({ item }: NewsRiskHeadlineCardProps) {
  const sourceLabel = item.source ?? item.feed_source ?? "—";
  const title = stripHtml(item.title) ?? item.title;
  const snippet = stripHtml(item.snippet);
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
        {title}
      </a>
      <p className={styles.meta}>
        {sourceLabel} · {formatDateTime(item.published_at)} ·{" "}
        {item.market || "—"}
      </p>
      {snippet ? <p className={styles.snippet}>{snippet}</p> : null}
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
