// frontend/trading-decision/src/components/NewsRiskHeadlineCard.tsx
import React from "react";
import type { NewsRadarItem } from "../api/types";
import styles from "./NewsRiskHeadlineCard.module.css";

interface Props {
  item: NewsRadarItem;
}

export const NewsRiskHeadlineCard: React.FC<Props> = ({ item }) => {
  const isHigh = item.severity === "high";

  return (
    <div
      className={`${styles.card} ${isHigh ? styles.severityHigh : ""}`}
      onClick={() => window.open(item.url, "_blank")}
    >
      <div className={styles.header}>
        <div className={styles.titleRow}>
          <span className={styles.title}>{item.title}</span>
          {item.included_in_briefing && (
            <span className={styles.briefingBadge}>IN BRIEFING</span>
          )}
        </div>
      </div>

      <div className={styles.meta}>
        <span className={styles.source}>{item.source || "Unknown"}</span>
        <span className={styles.dot}>•</span>
        <span className={styles.time}>
          {item.published_at
            ? new Date(item.published_at).toLocaleTimeString([], {
                hour: "2-digit",
                minute: "2-digit",
              })
            : "N/A"}
        </span>
      </div>

      {item.snippet && <div className={styles.snippet}>{item.snippet}</div>}

      <div className={styles.footer}>
        <div className={styles.themes}>
          {item.themes.map((theme) => (
            <span key={theme} className={styles.themeChip}>
              {theme}
            </span>
          ))}
          {item.symbols.map((symbol) => (
            <span key={symbol} className={styles.symbolChip}>
              {symbol}
            </span>
          ))}
        </div>
        {isHigh && <div className={styles.highSeverityLabel}>HIGH SEVERITY</div>}
      </div>
    </div>
  );
};
