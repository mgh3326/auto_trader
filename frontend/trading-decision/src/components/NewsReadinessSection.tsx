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
        aria-label="뉴스 준비도"
        className={styles.section}
        data-testid="news-readiness-section"
      >
        <header className={styles.header}>
          <h2>뉴스 준비도</h2>
          <ReadinessStatusBadge status="unavailable" />
        </header>
        <p className={styles.muted}>
          뉴스 준비도 조회에 실패했습니다. 뉴스를 미사용으로 간주하세요.
        </p>
      </section>
    );
  }

  const sourceEntries = Object.entries(news.source_counts);
  const sourceCoverage = news.source_coverage ?? [];

  return (
    <section
      aria-label="뉴스 준비도"
      className={styles.section}
      data-testid="news-readiness-section"
    >
      <header className={styles.header}>
        <h2>뉴스 준비도</h2>
        <ReadinessStatusBadge status={news.status} />
      </header>

      <dl className={styles.meta}>
        <div>
          <dt>최근 실행</dt>
          <dd>{formatDateTime(news.latest_finished_at)}</dd>
        </div>
        <div>
          <dt>최근 기사</dt>
          <dd>{formatDateTime(news.latest_article_published_at)}</dd>
        </div>
        <div>
          <dt>신선도 기준</dt>
          <dd>{news.max_age_minutes}분</dd>
        </div>
      </dl>

      {news.status !== "ready" ? (
        <p className={styles.warningLine} role="status">
          {news.status === "stale"
            ? `뉴스가 ${news.max_age_minutes}분 이상 경과했습니다. 행동 전에 확인하세요.`
            : "뉴스 파이프라인의 최근 실행 성공 기록이 없습니다."}
        </p>
      ) : null}

      {sourceEntries.length > 0 ? (
        <ul aria-label="뉴스 소스 건수" className={styles.sourceList}>
          {sourceEntries.map(([source, count]) => (
            <li className={styles.sourceChip} key={source}>
              {source}: {count}
            </li>
          ))}
        </ul>
      ) : (
        <p className={styles.muted}>소스 건수가 없습니다.</p>
      )}

      {sourceCoverage.length > 0 ? (
        <div className={styles.coverageTableWrap}>
          <h3 className={styles.previewHeading}>소스 커버리지</h3>
          <table className={styles.coverageTable}>
            <thead>
              <tr>
                <th>소스</th>
                <th>상태</th>
                <th>예상</th>
                <th>저장됨</th>
                <th>24시간</th>
                <th>최근 기사</th>
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
        최근 기사 ({preview.length})
      </h3>
      {preview.length === 0 ? (
        <p className={styles.muted}>미리 볼 최근 기사가 없습니다.</p>
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
