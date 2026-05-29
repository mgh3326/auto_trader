import "./screener.css";
import type { ScreenerPreset } from "../../types/screener";

interface Props {
  presets: ScreenerPreset[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}

function PresetItem({
  preset,
  active,
  onSelect,
}: {
  preset: ScreenerPreset;
  active: boolean;
  onSelect: (id: string) => void;
}) {
  // ROB-359 Scope B: surface honest divergence from Toss without fabricating it.
  const parityLabel =
    preset.parityStatus === "partial"
      ? "일부"
      : preset.parityStatus === "mismatch"
        ? "차이"
        : null;
  return (
    <li>
      <button
        type="button"
        data-testid={`screener-preset-${preset.id}`}
        className={active ? "screener-preset-item-active" : "screener-preset-item"}
        onClick={() => onSelect(preset.id)}
        aria-current={active ? "true" : undefined}
        title={preset.parityNote ?? undefined}
      >
        <span>{preset.name}</span>
        <span className="screener-preset-tags">
          {preset.badges.includes("인기") && (
            <span className="screener-preset-badge">인기</span>
          )}
          {parityLabel && (
            <span
              className="screener-preset-parity"
              data-parity={preset.parityStatus ?? undefined}
            >
              {parityLabel}
            </span>
          )}
        </span>
      </button>
    </li>
  );
}

export function ScreenerPresetSidebar({ presets, selectedId, onSelect }: Props) {
  // auto_trader-original presets are NOT made by Toss; group them separately so
  // the "토스증권이 만든" heading stays truthful. Unknown origin defaults to the
  // Toss group to preserve prior behavior during the additive rollout.
  const tossPresets = presets.filter((p) => p.presetOrigin !== "auto_trader_original");
  const ownPresets = presets.filter((p) => p.presetOrigin === "auto_trader_original");
  return (
    <div className="screener-sidebar" aria-label="주식 골라보기 목록">
      <div className="screener-sidebar-section">
        <div className="screener-sidebar-heading">내가 만든</div>
        <button type="button" className="screener-sidebar-link" disabled aria-disabled="true">
          직접 만들기 (준비중)
        </button>
      </div>
      {tossPresets.length > 0 && (
        <div className="screener-sidebar-section">
          <div className="screener-sidebar-heading">토스증권이 만든</div>
          <ul className="screener-preset-list">
            {tossPresets.map((p) => (
              <PresetItem
                key={p.id}
                preset={p}
                active={p.id === selectedId}
                onSelect={onSelect}
              />
            ))}
          </ul>
        </div>
      )}
      {ownPresets.length > 0 && (
        <div className="screener-sidebar-section">
          <div className="screener-sidebar-heading">auto_trader가 만든</div>
          <ul className="screener-preset-list">
            {ownPresets.map((p) => (
              <PresetItem
                key={p.id}
                preset={p}
                active={p.id === selectedId}
                onSelect={onSelect}
              />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
