// frontend/trading-decision/src/components/NewsRadarSummary.tsx
import type { NewsRadarReadiness, NewsRadarSummary as Summary } from "../api/types";
import { formatDateTime } from "../format/datetime";
import styles from "./NewsRadarSummary.module.css";

export interface NewsRadarSummaryProps {
  readiness: NewsRadarReadiness;
  summary: Summary;
  onRefresh: () => void;
}

const STATUS_LABEL: Record<NewsRadarReadiness["status"], string> = {
  ready: "Ready",
  stale: "Stale",
  unavailable: "Unavailable",
};

export default function NewsRadarSummary({
  readiness,
  summary,
  onRefresh,
}: NewsRadarSummaryProps) {
  return (
    <section
      aria-label="News radar summary"
      className={styles.section}
      data-testid="news-radar-summary"
    >
      <div className={styles.row}>
        <div>
          <h2>Market Risk News Radar</h2>
          <p className={styles.subtitle}>
            Read-only view of collected high-risk market news.
          </p>
        </div>
        <button className="btn" onClick={onRefresh} type="button">
          Refresh
        </button>
      </div>
      <ul className={styles.chips} aria-label="readiness chips">
        <li className={`${styles.chip} ${styles[`status_${readiness.status}`]}`}>
          {STATUS_LABEL[readiness.status]}
        </li>
        <li className={styles.chip}>
          Latest scraped: {formatDateTime(readiness.latest_scraped_at)}
        </li>
        <li className={styles.chip}>
          Latest article: {formatDateTime(readiness.latest_published_at)}
        </li>
        <li className={styles.chip}>6h: {readiness.recent_6h_count}</li>
        <li className={styles.chip}>24h: {readiness.recent_24h_count}</li>
        <li className={styles.chip}>Sources: {readiness.source_count}</li>
      </ul>
      <ul className={styles.chips} aria-label="summary chips">
        <li className={styles.chip}>High-risk: {summary.high_risk_count}</li>
        <li className={styles.chip}>Total: {summary.total_count}</li>
        <li className={styles.chip}>
          In briefing: {summary.included_in_briefing_count}
        </li>
        <li className={styles.chip}>
          Excluded: {summary.excluded_but_collected_count}
        </li>
      </ul>
    </section>
  );
}
