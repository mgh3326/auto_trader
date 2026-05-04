import type {
  PreopenNewsArticlePreview,
  PreopenNewsReadinessSummary,
} from "../api/types";
import { formatDateTime } from "../format/datetime";
import ReadinessStatusBadge from "./ReadinessStatusBadge";
import styles from "./NewsReadinessSection.module.css";

export interface NewsReadinessSectionProps {
  news: PreopenNewsReadinessSummary | null;
  preview: PreopenNewsArticlePreview[];
}

export default function NewsReadinessSection({
  news,
  preview,
}: NewsReadinessSectionProps) {
  if (news === null) {
    return (
      <section
        aria-label="News readiness"
        className={styles.section}
        data-testid="news-readiness-section"
      >
        <header className={styles.header}>
          <h2>News readiness</h2>
          <ReadinessStatusBadge status="unavailable" />
        </header>
        <p className={styles.muted}>
          News readiness lookup failed. Treat this preopen as if news is
          unavailable.
        </p>
      </section>
    );
  }

  const sourceEntries = Object.entries(news.source_counts);
  const sourceCoverage = news.source_coverage ?? [];

  return (
    <section
      aria-label="News readiness"
      className={styles.section}
      data-testid="news-readiness-section"
    >
      <header className={styles.header}>
        <h2>News readiness</h2>
        <ReadinessStatusBadge status={news.status} />
      </header>

      <dl className={styles.meta}>
        <div>
          <dt>Latest run</dt>
          <dd>{formatDateTime(news.latest_finished_at)}</dd>
        </div>
        <div>
          <dt>Latest article</dt>
          <dd>{formatDateTime(news.latest_article_published_at)}</dd>
        </div>
        <div>
          <dt>Freshness window</dt>
          <dd>{news.max_age_minutes} min</dd>
        </div>
      </dl>

      {news.status !== "ready" ? (
        <p className={styles.warningLine} role="status">
          {news.status === "stale"
            ? `News is older than ${news.max_age_minutes} min — verify before acting.`
            : "News pipeline did not report a recent successful run."}
        </p>
      ) : null}

      {sourceEntries.length > 0 ? (
        <ul aria-label="News source counts" className={styles.sourceList}>
          {sourceEntries.map(([source, count]) => (
            <li className={styles.sourceChip} key={source}>
              {source}: {count}
            </li>
          ))}
        </ul>
      ) : (
        <p className={styles.muted}>No source counts available.</p>
      )}

      {sourceCoverage.length > 0 ? (
        <div className={styles.coverageTableWrap}>
          <h3 className={styles.previewHeading}>Source coverage</h3>
          <table className={styles.coverageTable}>
            <thead>
              <tr>
                <th>Source</th>
                <th>Status</th>
                <th>Expected</th>
                <th>Stored</th>
                <th>24h</th>
                <th>Latest article</th>
              </tr>
            </thead>
            <tbody>
              {sourceCoverage.map((source) => (
                <tr key={source.feed_source}>
                  <td>{source.feed_source}</td>
                  <td>{source.status}</td>
                  <td>{source.expected_count}</td>
                  <td>{source.stored_total}</td>
                  <td>{source.recent_24h}</td>
                  <td>{formatDateTime(source.latest_published_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      <h3 className={styles.previewHeading}>
        Latest articles ({preview.length})
      </h3>
      {preview.length === 0 ? (
        <p className={styles.muted}>No recent articles to preview.</p>
      ) : (
        <ul className={styles.previewList}>
          {preview.map((item) => (
            <li className={styles.previewItem} key={item.id}>
              <a href={item.url} rel="noreferrer noopener" target="_blank">
                {item.title}
              </a>
              <span className={styles.previewMeta}>
                {item.source ?? item.feed_source ?? "—"} ·{" "}
                {formatDateTime(item.published_at)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
