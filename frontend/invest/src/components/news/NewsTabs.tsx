import type { FeedTab } from "../../types/feedNews";

export const PRIMARY_NEWS_TABS: { key: FeedTab; label: string }[] = [
  { key: "holdings", label: "보유주식" },
  { key: "watchlist", label: "관심주식" },
  { key: "top", label: "주요뉴스" },
  { key: "latest", label: "최신뉴스" },
  { key: "hot", label: "급상승뉴스" },
];

export const SECONDARY_NEWS_TABS: { key: FeedTab; label: string }[] = [
  { key: "kr", label: "국내" },
  { key: "us", label: "해외" },
  { key: "crypto", label: "크립토" },
];

export const NEWS_TABS: { key: FeedTab; label: string }[] = [
  ...PRIMARY_NEWS_TABS,
  ...SECONDARY_NEWS_TABS,
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
  const visibleTabs = PRIMARY_NEWS_TABS;

  if (variant === "pill-row") {
    return (
      <div
        data-testid="news-tabs"
        style={{ display: "flex", gap: 6, overflowX: "auto", paddingBottom: 4 }}
      >
        {visibleTabs.map((t) => {
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
      {visibleTabs.map((t) => {
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
