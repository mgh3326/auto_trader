import "./screener.css";
import type { ScreenerPreset } from "../../types/screener";

interface Props {
  presets: ScreenerPreset[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}

export function ScreenerPresetSidebar({ presets, selectedId, onSelect }: Props) {
  return (
    <div className="screener-sidebar" aria-label="주식 골라보기 목록">
      <div className="screener-sidebar-section">
        <div className="screener-sidebar-heading">내가 만든</div>
        <button type="button" className="screener-sidebar-link" disabled aria-disabled="true">
          직접 만들기 (준비중)
        </button>
      </div>
      <div className="screener-sidebar-section">
        <div className="screener-sidebar-heading">토스증권이 만든</div>
        <ul className="screener-preset-list">
          {presets.map((p) => {
            const active = p.id === selectedId;
            return (
              <li key={p.id}>
                <button
                  type="button"
                  data-testid={`screener-preset-${p.id}`}
                  className={active ? "screener-preset-item-active" : "screener-preset-item"}
                  onClick={() => onSelect(p.id)}
                  aria-current={active ? "true" : undefined}
                >
                  <span>{p.name}</span>
                  {p.badges.includes("인기") && (
                    <span className="screener-preset-badge">인기</span>
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}
