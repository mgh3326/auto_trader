// frontend/trading-decision/src/pages/NewsRadarPage.tsx
import ErrorView from "../components/ErrorView";
import LoadingView from "../components/LoadingView";
import NewsRadarFilterBar from "../components/NewsRadarFilterBar";
import NewsRadarSection from "../components/NewsRadarSection";
import NewsRadarSummary from "../components/NewsRadarSummary";
import NewsRiskHeadlineCard from "../components/NewsRiskHeadlineCard";
import { useNewsRadar } from "../hooks/useNewsRadar";
import styles from "./NewsRadarPage.module.css";

export default function NewsRadarPage() {
  const radar = useNewsRadar();

  if (radar.status === "loading" && radar.data === null) {
    return <LoadingView />;
  }
  if (radar.status === "error") {
    return (
      <main className={styles.page}>
        <ErrorView
          message={radar.error ?? "Failed to load news radar."}
          onRetry={radar.refetch}
        />
      </main>
    );
  }

  const data = radar.data;
  if (!data) return <LoadingView />;

  const highRiskHeadlines = data.items
    .filter((item) => item.severity === "high")
    .slice(0, 6);

  return (
    <main className={styles.page}>
      <NewsRadarSummary
        readiness={data.readiness}
        summary={data.summary}
        onRefresh={radar.refetch}
      />
      <NewsRadarFilterBar filters={radar.filters} onChange={radar.setFilters} />

      {highRiskHeadlines.length > 0 ? (
        <section
          aria-label="High-risk headlines"
          className={styles.headlineStrip}
        >
          <h3>High-risk headlines</h3>
          <div className={styles.headlineGrid}>
            {highRiskHeadlines.map((item) => (
              <NewsRiskHeadlineCard item={item} key={item.id} />
            ))}
          </div>
        </section>
      ) : null}

      {data.sections.length === 0 && data.items.length === 0 ? (
        <p className={styles.empty}>
          No matching news in this window. Try widening the time range or
          clearing filters.
        </p>
      ) : (
        data.sections.map((section) => (
          <NewsRadarSection key={section.section_id} section={section} />
        ))
      )}

      {data.excluded_items.length > 0 ? (
        <section
          aria-label="Collected but excluded"
          className={styles.excluded}
          data-testid="news-radar-excluded"
        >
          <h3>Collected but excluded from briefing</h3>
          <div className={styles.headlineGrid}>
            {data.excluded_items.map((item) => (
              <NewsRiskHeadlineCard item={item} key={`ex-${item.id}`} />
            ))}
          </div>
        </section>
      ) : null}
    </main>
  );
}
