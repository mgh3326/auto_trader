import type { AssetCategory } from "../types/invest";

export type AssetCategoryKey = AssetCategory | "all";

const LABELS: Record<AssetCategoryKey, string> = {
  all: "전체",
  kr_stock: "한국주식",
  us_stock: "해외주식",
  crypto: "코인",
};

export function AssetCategoryFilter({
  active,
  onChange,
  disabledCategories = [],
}: {
  active: AssetCategoryKey;
  onChange: (c: AssetCategoryKey) => void;
  disabledCategories?: AssetCategoryKey[];
}) {
  const options: AssetCategoryKey[] = ["all", "kr_stock", "us_stock", "crypto"];

  return (
    <div style={{ display: "flex", gap: 6, padding: "0 16px", flexWrap: "wrap" }}>
      {options.map((c) => {
        const on = c === active;
        const disabled = disabledCategories.includes(c);
        return (
          <button
            key={c}
            type="button"
            onClick={() => !disabled && onChange(c)}
            style={{
              padding: "6px 12px",
              borderRadius: 20,
              background: on ? "var(--text)" : "var(--surface-2)",
              color: on ? "var(--bg)" : "var(--text)",
              border: "1px solid",
              borderColor: on ? "var(--text)" : "transparent",
              fontSize: 12,
              fontWeight: 500,
              cursor: disabled ? "not-allowed" : "pointer",
              opacity: disabled ? 0.3 : 1,
              transition: "all 0.2s",
            }}
          >
            {LABELS[c]}
          </button>
        );
      })}
    </div>
  );
}
