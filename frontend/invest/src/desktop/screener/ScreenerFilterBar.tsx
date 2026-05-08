import "./screener.css";
import type { ScreenerFilterChip } from "../../types/screener";

interface Props {
  title: string;
  description: string;
  chips: ScreenerFilterChip[];
  resultCount: number;
  onOpenFilterModal: () => void;
}

export function ScreenerFilterBar({
  title, description, chips, resultCount, onOpenFilterModal,
}: Props) {
  return (
    <div className="screener-filter-bar">
      <div>
        <h2 className="screener-filter-title">{title}</h2>
        <p className="screener-filter-description">{description}</p>
      </div>
      <div className="screener-chip-row">
        <button
          type="button"
          className="screener-chip-add"
          onClick={onOpenFilterModal}
          data-testid="screener-add-filter"
        >
          + 필터추가
        </button>
        {chips.map((c, i) => (
          <span className="screener-chip" key={`${c.label}-${i}`}>
            <strong>{c.label}</strong>
            {c.detail && <span className="screener-chip-detail">· {c.detail}</span>}
          </span>
        ))}
      </div>
      <div className="screener-result-count">
        검색된 주식 ・ <strong>{resultCount.toLocaleString()}</strong>개
      </div>
    </div>
  );
}
