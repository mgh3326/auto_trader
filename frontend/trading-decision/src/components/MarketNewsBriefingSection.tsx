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

const SUMMARY_CHIP_LABEL: Record<SummaryKey, string> = {
  included: "포함",
  excluded: "제외",
  sections: "섹션",
  uncategorized: "미분류",
};

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
          <span className={styles.score}>점수 {relevance.score}</span>
          {matchedTerms.length > 0 ? (
            <span className={styles.terms}>매칭 키워드: {matchedTerms.join(", ")}</span>
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
      <section aria-label="시장 뉴스 브리핑" className={styles.section}>
        <header className={styles.header}>
          <h2>시장 뉴스 브리핑</h2>
        </header>
        <p className={styles.muted}>아직 시장 뉴스 브리핑이 없습니다.</p>
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
    <section aria-label="시장 뉴스 브리핑" className={styles.section}>
      <header className={styles.header}>
        <div>
          <h2>시장 뉴스 브리핑</h2>
          <p className={styles.subtitle}>
            최근 뉴스에서 시장 관련 섹션을 추출해 트레이딩 리뷰 전에 필터링했습니다.
          </p>
        </div>
      </header>

      {summaryChips.length > 0 ? (
        <ul aria-label="시장 뉴스 브리핑 요약" className={styles.summaryChips}>
          {summaryChips.map(([key, value]) => (
            <li className={styles.summaryChip} key={key}>
              <span>{SUMMARY_CHIP_LABEL[key]}</span>
              <strong>{value}</strong>
            </li>
          ))}
        </ul>
      ) : null}

      {briefing.sections.length === 0 ? (
        <p className={styles.muted}>시그널이 강한 브리핑 섹션이 없습니다.</p>
      ) : (
        <div className={styles.sectionGrid}>
          {briefing.sections.map((section) => (
            <article className={styles.card} key={section.section_id}>
              <header className={styles.cardHeader}>
                <h3>{section.title}</h3>
                <span className={styles.count}>{section.items.length}건</span>
              </header>
              {section.items.length === 0 ? (
                <p className={styles.muted}>이 섹션에는 기사가 없습니다.</p>
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
        필터링된 노이즈: {briefing.excluded_count}
      </div>
      {briefing.top_excluded.length > 0 ? (
        <details className={styles.excludedDetails}>
          <summary>상위 제외 기사 보기 ({briefing.top_excluded.length})</summary>
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
