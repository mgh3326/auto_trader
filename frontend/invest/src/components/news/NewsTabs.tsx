import type { FeedTab } from "../../types/feedNews";

export const NEWS_TABS: { key: FeedTab; label: string }[] = [
  { key: "top", label: "주요" },
  { key: "latest", label: "최신" },
  { key: "hot", label: "핫이슈" },
  { key: "holdings", label: "보유" },
  { key: "watchlist", label: "관심" },
  { key: "kr", label: "국내" },
  { key: "us", label: "해외" },
  { key: "crypto", label: "크립토" },
];

export function NewsTabs({
  value,
  onChange,
  variant = "underline",
}: {
  value: FeedTab;
  onChange: (tab: FeedTab) => void;
  variant?: "underline" | "pill-row";
}) {
  if (variant === "pill-row") {
    return (
      <div
        data-testid="news-tabs"
        style={{ display: "flex", gap: 6, overflowX: "auto", paddingBottom: 4 }}
      >
        {NEWS_TABS.map((t) => {
          const active = t.key === value;
          return (
            <button
              key={t.key}
              data-testid={`tab-${t.key}`}
              onClick={() => onChange(t.key)}
              style={{
                flex: "0 0 auto",
                padding: "6px 12px",
                border: "none",
                borderRadius: 999,
                cursor: "pointer",
                background: active ? "var(--fg)" : "var(--surface-2)",
                color: active ? "var(--bg)" : "var(--fg-2)",
                fontWeight: 600,
                fontSize: 12,
                fontFamily: "inherit",
                whiteSpace: "nowrap",
              }}
            >
              {t.label}
            </button>
          );
        })}
      </div>
    );
  }

  // Underline tab strip per the bundle's NewsView.jsx.
  return (
    <div
      data-testid="news-tabs"
      style={{
        display: "flex",
        gap: 4,
        borderBottom: "1px solid var(--divider)",
        overflowX: "auto",
      }}
    >
      {NEWS_TABS.map((t) => {
        const active = t.key === value;
        return (
          <button
            key={t.key}
            data-testid={`tab-${t.key}`}
            onClick={() => onChange(t.key)}
            style={{
              padding: "10px 14px",
              border: "none",
              background: "transparent",
              color: active ? "var(--fg)" : "var(--fg-3)",
              borderBottom: `2px solid ${active ? "var(--fg)" : "transparent"}`,
              fontWeight: 600,
              fontSize: 14,
              fontFamily: "inherit",
              cursor: "pointer",
              marginBottom: -1,
              whiteSpace: "nowrap",
              flexShrink: 0,
            }}
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}
