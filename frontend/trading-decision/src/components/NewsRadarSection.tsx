// frontend/trading-decision/src/components/NewsRadarSection.tsx
import type { NewsRadarSection as Section } from "../api/types";
import NewsRiskHeadlineCard from "./NewsRiskHeadlineCard";
import styles from "./NewsRadarSection.module.css";

export interface NewsRadarSectionProps {
  section: Section;
}

export default function NewsRadarSection({ section }: NewsRadarSectionProps) {
  return (
    <section
      aria-label={`Radar section ${section.section_id}`}
      className={`${styles.section} ${styles[`severity_${section.severity}`]}`}
      data-testid={`news-radar-section-${section.section_id}`}
    >
      <header className={styles.header}>
        <h3>{section.title}</h3>
        <span className={styles.count}>{section.items.length} items</span>
      </header>
      {section.items.length === 0 ? (
        <p className={styles.muted}>No items in this section.</p>
      ) : (
        <div className={styles.grid}>
          {section.items.map((item) => (
            <NewsRiskHeadlineCard item={item} key={item.id} />
          ))}
        </div>
      )}
    </section>
  );
}
