import type { AssetCategoryKey } from "../AssetCategoryFilter";

const OPTIONS: { key: AssetCategoryKey; label: string }[] = [
  { key: "all", label: "전체" },
  { key: "kr_stock", label: "한국주식" },
  { key: "us_stock", label: "해외주식" },
  { key: "crypto", label: "코인" },
];

export function FilterChips({
  value,
  onChange,
}: {
  value: AssetCategoryKey;
  onChange: (v: AssetCategoryKey) => void;
}) {
  return (
    <div data-testid="home-filter-chips" style={{ display: "flex", gap: 6 }}>
      {OPTIONS.map(({ key, label }) => {
        const on = value === key;
        return (
          <button
            key={key}
            type="button"
            onClick={() => onChange(key)}
            style={{
              padding: "6px 14px",
              borderRadius: 999,
              border: "none",
              cursor: "pointer",
              background: on ? "var(--fg)" : "var(--surface-2)",
              color: on ? "#fff" : "var(--fg-2)",
              fontWeight: 600,
              fontSize: 13,
              fontFamily: "inherit",
              whiteSpace: "nowrap",
              flexShrink: 0,
              transition: "all 120ms cubic-bezier(0.2,0,0,1)",
            }}
          >
            {label}
          </button>
        );
      })}
    </div>
  );
}
