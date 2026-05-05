// frontend/trading-decision/src/components/NewsRadarFilterBar.tsx
import type { ChangeEvent } from "react";
import type {
  NewsRadarFilters,
  NewsRadarMarket,
  NewsRadarRiskCategory,
} from "../api/types";
import styles from "./NewsRadarFilterBar.module.css";

export interface NewsRadarFilterBarProps {
  filters: NewsRadarFilters;
  onChange: (
    next: NewsRadarFilters | ((prev: NewsRadarFilters) => NewsRadarFilters),
  ) => void;
}

const MARKET_OPTIONS: NewsRadarMarket[] = ["all", "kr", "us", "crypto"];
const HOURS_OPTIONS = [6, 24, 72];
const CATEGORY_OPTIONS: { value: NewsRadarRiskCategory | ""; label: string }[] = [
  { value: "", label: "All categories" },
  { value: "geopolitical_oil", label: "Geopolitical / Oil" },
  { value: "macro_policy", label: "Macro / Policy" },
  { value: "crypto_security", label: "Crypto / Security" },
  { value: "earnings_bigtech", label: "Earnings / Big tech" },
  { value: "korea_market", label: "Korea market" },
];

export default function NewsRadarFilterBar({
  filters,
  onChange,
}: NewsRadarFilterBarProps) {
  return (
    <form
      aria-label="News radar filters"
      className={styles.bar}
      data-testid="news-radar-filters"
      onSubmit={(e) => e.preventDefault()}
    >
      <label>
        Market
        <select
          value={filters.market}
          onChange={(e: ChangeEvent<HTMLSelectElement>) =>
            onChange((prev) => ({
              ...prev,
              market: e.target.value as NewsRadarMarket,
            }))
          }
        >
          {MARKET_OPTIONS.map((m) => (
            <option key={m} value={m}>
              {m.toUpperCase()}
            </option>
          ))}
        </select>
      </label>
      <label>
        Window
        <select
          value={filters.hours}
          onChange={(e) =>
            onChange((prev) => ({ ...prev, hours: Number(e.target.value) }))
          }
        >
          {HOURS_OPTIONS.map((h) => (
            <option key={h} value={h}>
              {h}h
            </option>
          ))}
        </select>
      </label>
      <label>
        Category
        <select
          value={filters.riskCategory}
          onChange={(e) =>
            onChange((prev) => ({
              ...prev,
              riskCategory: e.target.value as NewsRadarRiskCategory | "",
            }))
          }
        >
          {CATEGORY_OPTIONS.map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
        </select>
      </label>
      <label className={styles.search}>
        Search
        <input
          type="text"
          value={filters.q}
          placeholder="UAE, Hormuz, Brent…"
          onChange={(e) =>
            onChange((prev) => ({ ...prev, q: e.target.value }))
          }
        />
      </label>
      <label className={styles.toggle}>
        <input
          type="checkbox"
          checked={filters.includeExcluded}
          onChange={(e) =>
            onChange((prev) => ({
              ...prev,
              includeExcluded: e.target.checked,
            }))
          }
        />
        Show collected-but-excluded
      </label>
    </form>
  );
}
